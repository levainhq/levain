"""ONESURFACE OS-2, the dispatch family through the sheet: headless Chrome (its own profile, ``_drive``) on a real cockpit
server (make_server + a Signer on rpId localhost pinned to the page), a real authority log holding a laptop key and a
passkey enrolled for localhost, a real dispatch job store in the Signer's queue directory, and Chrome's virtual
authenticator holding the passkey's private key. A tap in the sheet runs the phone port's own dispatch ceremony on the
cockpit and the job store verifies the signature (ssh-keygen and its passkey checks): the job ends ``signed`` /
``cancelled`` in the store itself.

Reproduced red before the build: ``/sign/dispatch/<job>`` was 404 and the Decisions view listed no dispatch job."""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.primitives import serialization

from levain.authority import anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.answering import passkey_keys
from levain.consent.queue import authority_source
from levain.consent.sign import Signer
from levain.dispatch.jobs import JobStore
from levain.web_server import make_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_card import DESKTOP, TOK, _free_port, needs_ssh
from tests.test_cockpit_sign_origin import NS, _Passkey
from tests.test_dispatch_phone import file_job, repo  # noqa: F401  (repo is a fixture)
from tests._phone_fixtures import _enrol

STATE = "document.getElementById('sheet').dataset.state"
NOTE = "document.getElementById('sheet-note').textContent"
FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"


class _DispatchRig:
    """A dispatch job filed in the Signer's queue directory, signed with a passkey enrolled for localhost in the cockpit
    that directory's hook names."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, repo: Any) -> None:
        home = tmp_path / "levain_home"
        monkeypatch.setenv("LEVAIN_HOME", str(home))
        self.qdir = home / "consent"
        self.qdir.mkdir(parents=True, mode=0o700)
        laptop = tmp_path / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
        lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
        lfp = fingerprint_of(lpub)
        root = tmp_path / "ck"
        root.mkdir()
        CockpitDB(root).set_install_class("reported")
        self.pk = _Passkey("localhost")
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                         "class": "laptop-presence", "custody": "passphrase"})
        _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": self.pk.key.fingerprint,
                                         "public_key": self.pk.line, "class": "display-phone", "custody": "passkey"})
        conn.close()
        (self.qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                                "away_hold_seconds": 0}))
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
        self.job = file_job({"qdir": self.qdir, "repo": repo})
        self.port = _free_port()
        self.origin = f"http://localhost:{self.port}"
        self.signer = Signer(self.qdir, (NS,), "localhost",
                             lambda: {k.fingerprint: k for k in passkey_keys(source(), "localhost")})
        self.signer.page_origin = self.origin
        _root, self.src, _ids = _install(tmp_path)

    def state(self) -> str:
        return JobStore(self.qdir).get(self.job.id).state

    @contextmanager
    def serving(self) -> Iterator[Any]:
        httpd = make_server(self.src, host="127.0.0.1", port=self.port, surface_tokens={"browser": TOK},
                            install_class="reported", signer=self.signer)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)

    def authenticator(self) -> dict:
        pkcs8 = self.pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
        return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                                  "credentialId": base64.b64encode(b"levain-dispatch-cred").decode(),
                                  "userHandle": base64.b64encode(b"levain-dispatch-user").decode()}}

    def open_top(self) -> list[dict]:
        """Put the cockpit's token in this browser's storage, then open the sheet top-level."""
        path = f"/sign/dispatch/{self.job.id}"
        return [{"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                         f"location.href = '{path}'; return 1; }})()"},
                {"until": f"location.pathname === '{path}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


@needs_browser
@needs_ssh
def test_a_dispatch_job_is_signed_in_the_sheet_and_the_store_verifies_it(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    with rig.serving() as httpd:
        shown = httpd.dispatch_sheet.row(rig.job.id)["sha256"]
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "document.getElementById('sheet-title').textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"sign\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "(document.getElementById('sheet-signing') || {}).textContent"},
        ])
    # fix 1029-2: the challenge names the version the sheet showed, as a plain sign request's does
    assert json.loads(out["posts"][0].split(" ", 1)[1]) == {"signer_fp": rig.pk.key.fingerprint,
                                                            "request_sha256": shown}, out["posts"][0]
    v = out["values"]
    assert v[1] == "open" and v[2].startswith("dispatch"), v
    assert v[3] == "sign,cancel", v
    assert str(v[5]).startswith("signed (signed) · by " + rig.pk.key.fingerprint), v
    assert str(v[6]).startswith("Signed: {"), v          # the server's line: the payload's canonical JSON
    assert rig.state() == "signed"                       # the job store itself verified it
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/challenge", f"/cockpit/dispatch/{rig.job.id}/answer"], posts


@needs_browser
@needs_ssh
def test_a_dispatch_job_is_cancelled_in_the_sheet_with_its_reason(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "(() => { const b = document.getElementById('sheet-reason'); b.value = '  wrong repo  '; return 1; })()"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"cancel\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    v = out["values"]
    assert str(v[4]).startswith("signed (cancelled) · by " + rig.pk.key.fingerprint), v
    assert rig.state() == "cancelled"
    job = JobStore(rig.qdir).get(rig.job.id)
    assert any("wrong repo" in (h.evidence or "") or "wrong repo" in (h.cause or "") for h in job.history), job.history
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/cancel-challenge", f"/cockpit/dispatch/{rig.job.id}/cancel"], posts


@needs_browser
@needs_ssh
def test_the_decisions_view_lists_the_job_and_its_sign_button_opens_the_sheet_with_sign_picked(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    sign = "document.querySelector('[data-dispatch-job] [data-dispatch-act=\"sign\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "!!(window.LevainNeeds && window.LevainNeeds.history)", "ms": 30000},
            {"eval": "window.LevainNeeds.history() || 1"},
            {"until": f"{sign} && 1", "ms": 30000},
            {"eval": f"{sign}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"{FRAME}.getAttribute('src')"},
            {"eval": f"!!{IN}.getElementById('sheet-picked') && {IN}.getElementById('sheet-picked').textContent"},
            {"eval": f"{IN}.getElementById('sheet-picked').click() || 1"},
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && "
                      f"{IN}.getElementById('sheet-note').textContent", "ms": 30000},
        ])
    v = out["values"]
    assert v[4] == "open", v
    assert v[5].startswith(f"/sign/dispatch/{rig.job.id}#v=") and v[5].endswith("&choice=sign"), v
    # the version the card passes is exactly the form the sheet's picker binds: the row's sha256, its first 16 hex
    vv = v[5].split("#v=", 1)[1].split("&", 1)[0]
    assert len(vv) == 16 and all(ch in "0123456789abcdef" for ch in vv), v
    assert v[6] == "Sign (sign)", v
    assert str(v[8]).startswith("signed (signed)"), v
    assert rig.state() == "signed"


# --- fix round 1 (L3 os2d r1) ------------------------------------------------------------------------------------------

def _wide_job(rig: _DispatchRig, repo: Any, paths: "list[str]") -> Any:  # noqa: F811
    """A job whose contract writes every path in ``paths`` (a template whose write set holds them), filed in rig's queue."""
    from datetime import datetime, timedelta, timezone

    from levain.cockpit.consent import utc_stamp
    from tests.test_dispatch_contract import POLICY, registry, submission, template

    path, base = repo
    sub = submission(path, base, write=paths, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    job, _cap = JobStore(rig.qdir).file(sub, resolve_template=registry(template(write_paths=frozenset(paths))),
                                        policy=POLICY, filer_session="sess-1", filer_agent="claude_code")
    return job


@needs_browser
@needs_ssh
def test_a_thirteen_path_contract_is_drawn_whole_in_a_scrolling_box(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """codex HIGH 1: every write path the digest binds is on the sheet Sign is offered under, none cut to an ellipsis;
    the box scrolls instead."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    paths = [f"src/n{i:02d}.py" for i in range(13)]
    rig.job = _wide_job(rig, repo, paths)
    box = "document.getElementById('sheet-contract')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, *rig.open_top(),
            {"eval": f"{box} && {box}.textContent"},
            {"eval": f"getComputedStyle({box}).overflowY"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
        ])
    v = out["values"]
    assert v[1] == "open", v
    text = str(v[2])
    assert all(p in text for p in paths) and "…" not in text, text
    assert v[3] == "auto" and v[4] == "sign,cancel", v


def _as_recommended(httpd: Any, job: Any, card: "dict[str, Any]") -> None:
    """The served job read as recommended with ``card`` as its authorise card (DispatchPhone.record, the card swapped)."""
    phone = httpd.dispatch_sheet.phone
    real = phone.record

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real(j, now, signers, note)
        if j.id == job.id:
            rec.update({"state": "recommended", "can_sign": False, "authorise": card})
        return rec

    phone.record = record


@needs_browser
@needs_ssh
def test_a_change_set_past_the_cap_draws_no_act_and_says_review_it_on_the_laptop(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """codex HIGH 2: a card whose ``change_overflow`` is set cannot be shown whole: the sheet draws no act (no
    Authorise, no Cancel, no bare "Sign with your passkey") and says it is reviewed on the laptop."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    fp = rig.pk.key.fingerprint
    changes = [{"path": f"src/c{i}.py", "status": "M", "added": 1, "deleted": 1, "binary": False, "gitlink": False}
               for i in range(3)]
    card = {"onto": "main", "run": 1, "tier": "T2", "changes": changes, "change_overflow": 60, "files": 63,
            "changed_lines": 126, "signers": [fp], "note": "", "problem": None}
    with rig.serving() as httpd:
        _as_recommended(httpd, rig.job, card)
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "document.getElementById('sheet-actions').querySelectorAll('button').length"},
            {"eval": "(document.getElementById('sheet-review') || {}).textContent || ''"},
        ])
    v = out["values"]
    assert v[1] == "review", v
    assert v[2] == 0, v
    assert "needs review on the laptop" in v[3] and f"levain dispatch show {rig.job.id}" in v[3], v
    assert out["posts"] == [] and rig.state() == "filed"


class _AuthoriseRig(_DispatchRig):
    """A REAL recommended job (tests/test_dispatch_adopt.LandWorld: a verified job, two passing runs, a recommend onto
    main naming run 1) in the Signer's queue directory, with a passkey enrolled for localhost in the cockpit its hook
    names, so the sheet's Authorise runs the phone port's own authorise ceremony."""

    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:  # noqa: D107 - no super(): another world
        from tests.test_dispatch_adopt import LandWorld

        monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain_home"))
        (tmp_path / "lw").mkdir()
        self.lw = lw = LandWorld(tmp_path / "lw")
        self.pk = _Passkey("localhost")
        lw.log.link({"op": "enrol", "fingerprint": self.pk.key.fingerprint, "public_key": self.pk.line,
                     "class": "display-phone", "custody": "passkey"})
        job, cap = lw.verified()
        self.job = lw.jstore.recommend(job.id, cap, onto="main", run=1)
        self.qdir = lw.ddir
        self.port = _free_port()
        self.origin = f"http://localhost:{self.port}"
        src = lw.log.source
        self.signer = Signer(self.qdir, (NS,), "localhost",
                             lambda: {k.fingerprint: k for k in passkey_keys(src(), "localhost")})
        self.signer.page_origin = self.origin
        _root, self.src, _ids = _install(tmp_path)


@needs_browser
@needs_ssh
def test_an_authorise_whose_recommendation_moved_between_card_and_tap_signs_nothing(tmp_path: Path, monkeypatch) -> None:
    """complement MED 3: the sheet shows run 1 onto main; between the card and the tap the recommendation moves to run
    0. The authorise challenge the server builds is for run 0: the sheet compares it with the card BEFORE asking the
    passkey, signs nothing, says so, and reads the job again (its new version, its new card).

    Since fix 1029-2 the server refuses this first (the version moved with the card); the server's compare is switched
    off here so the sheet's own card compare, its second line, is the one tested."""
    rig = _AuthoriseRig(tmp_path, monkeypatch)
    lw = rig.lw
    main_before = lw.main()
    box = "document.getElementById('sheet-contract')"
    with rig.serving() as httpd:
        sheet = httpd.dispatch_sheet
        sheet.phone.shown = lambda *_a, **_k: None          # the server's version compare off: the sheet's card compare alone
        real_step = sheet.step

        def step(job_id: str, action: str, req: Any) -> Any:
            # the recommendation moves just before the tap's challenge is built
            if action == "authorise-challenge":
                sheet.phone._proposal = lambda _store, _job: ("main", 0)
            return real_step(job_id, action, req)

        sheet.step = step
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "(() => { window.__gets = 0; const c = navigator.credentials, g = c.get.bind(c); "
                     "c.get = (o) => { window.__gets++; return g(o); }; return 1; })()"},
            {"eval": f"/Authorise run 1 onto main/.test({box}.textContent)"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"authorise\"]').click() || 1"},
            {"until": f"/nothing was signed/.test({NOTE}) && {STATE} === 'open' && "
                      f"/Authorise run 0 onto main/.test({box}.textContent) && {NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"authorise\"]').textContent"},
        ])
    v = out["values"]
    assert v[1] == "open" and v[3] is True and v[4] == "cancel,authorise", v
    assert "run 0 onto main" in v[6] and "run 1 onto main" in v[6] and "nothing was signed" in v[6], v
    assert v[7] == 0, v                                            # no passkey was ever asked
    assert v[8] == "(authorise) Authorise run 0 onto main", v      # the sheet now shows the moved card
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/authorise-challenge"], posts
    assert lw.jstore.get(rig.job.id).state == "recommended" and lw.main() == main_before


@needs_browser
@needs_ssh
def test_an_authorise_on_the_card_shown_lands_it(tmp_path: Path, monkeypatch) -> None:
    """The check above passes the card it was shown: the payload's run (a string) and branch and the challenge's tier
    are the card's, so the passkey is asked and the land goes through."""
    rig = _AuthoriseRig(tmp_path, monkeypatch)
    lw = rig.lw
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"authorise\"]').click() || 1"},
            {"until": f"/^(signed|Not)/.test({NOTE}) && {NOTE}", "ms": 60000},
        ])
    v = out["values"]
    assert str(v[3]).startswith("signed (adopted, adopted)"), v
    assert lw.main() == lw.jstore.run_record(rig.job.id, 1).staged_commit


# --- fix 1029-2 (complement, L3 of 62b45ef6..680fee95): the version the sheet showed is what the cockpit challenges ------

GETS = ("(() => { window.__gets = 0; const c = navigator.credentials, g = c.get.bind(c); "
        "c.get = (o) => { window.__gets++; return g(o); }; return 1; })()")


def _amend_at_first(httpd: Any, rig: Any, action: str) -> "dict[str, Any]":
    """The job reads as amended (another contract digest) from the first ``action`` post on: the change lands between the
    sheet's read and the tap's challenge. Returns the box whose ``"at"`` holds the version the sheet had shown."""
    sheet = httpd.dispatch_sheet
    phone, real_step, real_record = sheet.phone, sheet.step, sheet.phone.record
    seen: "dict[str, Any]" = {}

    def record(j: Any, now: str, signers: "list[str]", note: str) -> "dict[str, Any]":
        rec = real_record(j, now, signers, note)
        if seen.get("at") and j.id == rig.job.id:
            rec["digest"] = "d" * 64
        return rec

    def step(job_id: str, act: str, req: Any) -> Any:
        if act == action and "at" not in seen:
            seen["at"] = sheet.row(job_id)["sha256"]
        return real_step(job_id, act, req)

    phone.record = record
    sheet.step = step
    return seen


@needs_browser
@needs_ssh
def test_a_dispatch_job_amended_between_the_read_and_the_tap_signs_nothing_and_is_drawn_again(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """Reproduced red before the fix: the sheet posted {signer_fp} alone, the cockpit built the challenge for the job as
    amended, and the passkey signed it. Now the challenge names the version shown, the cockpit refuses it (409), no
    passkey is asked, the sheet reads the job again, and a tap on the version now shown signs it."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    sign = "document.querySelector('#sheet-actions [data-choice=\"sign\"]')"
    with rig.serving() as httpd:
        seen = _amend_at_first(httpd, rig, "challenge")
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(), {"eval": GETS},
            {"eval": f"{sign}.click() || 1"},
            {"until": f"/changed since it was shown/.test({NOTE}) && {STATE} === 'open' && {sign} && !{sign}.disabled "
                      f"&& {NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": f"{sign}.click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    v = out["values"]
    assert "nothing was signed" in v[4], v
    assert v[5] == 0, v                                     # no passkey was asked for the amended job
    posts = [p.split(" ", 1) for p in out["posts"]]
    assert [p[0] for p in posts] == [f"/cockpit/dispatch/{rig.job.id}/challenge"] * 2 + [
        f"/cockpit/dispatch/{rig.job.id}/answer"], posts
    first, second = json.loads(posts[0][1]), json.loads(posts[1][1])
    assert first["request_sha256"] == seen["at"] and second["request_sha256"] != seen["at"], (first, second)
    assert rig.state() == "signed"


@needs_browser
@needs_ssh
def test_a_redraw_that_cannot_be_read_says_so_and_keeps_the_controls_locked(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """complement (same review): redraw() had no catch, so a read that failed left the sheet with no word of it. The read
    after the refusal throws here: the refusal stays said, the failure is said beside it, and no control is live."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    sign = "document.querySelector('#sheet-actions [data-choice=\"sign\"]')"
    with rig.serving() as httpd:
        _amend_at_first(httpd, rig, "challenge")
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            # every read of the job after the first throws at once (the page's fetch, not the server)
            {"eval": "(() => { const f = window.fetch; window.fetch = (u, o) => { if (String(u).indexOf('/sheet.json') >= 0) "
                     "throw new TypeError('no read'); return f(u, o); }; return 1; })()"},
            {"eval": f"{sign}.click() || 1"},
            {"until": f"/could not be drawn again/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#sheet-actions button, #sheet-actions input')].every(c => c.disabled)"},
        ])
    v = out["values"]
    assert "changed since it was shown" in v[4] and "nothing was signed" in v[4] and "reload this page" in v[4], v
    assert v[5] is True, v
    assert rig.state() == "filed"


@needs_browser
@needs_ssh
def test_an_authorise_whose_card_moved_is_refused_by_the_cockpit_before_any_passkey(tmp_path: Path, monkeypatch) -> None:
    """The server's compare on: the recommendation moves between the card and the tap, the authorise challenge names the
    card's version, the cockpit refuses it, no passkey is asked, and the sheet draws the moved card."""
    rig = _AuthoriseRig(tmp_path, monkeypatch)
    lw = rig.lw
    main_before = lw.main()
    box = "document.getElementById('sheet-contract')"
    with rig.serving() as httpd:
        sheet = httpd.dispatch_sheet
        real_step = sheet.step

        def step(job_id: str, action: str, req: Any) -> Any:
            if action == "authorise-challenge":
                sheet.phone._proposal = lambda _store, _job: ("main", 0)
            return real_step(job_id, action, req)

        sheet.step = step
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(), {"eval": GETS},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"authorise\"]').click() || 1"},
            {"until": f"/changed since it was shown/.test({NOTE}) && {STATE} === 'open' && "
                      f"/Authorise run 0 onto main/.test({box}.textContent) && {NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
        ])
    v = out["values"]
    assert "nothing was signed" in v[4], v
    assert v[5] == 0, v
    posts = [p.split(" ", 1) for p in out["posts"]]
    assert [p[0] for p in posts] == [f"/cockpit/dispatch/{rig.job.id}/authorise-challenge"], posts
    assert "request_sha256" in json.loads(posts[0][1]), posts
    assert lw.jstore.get(rig.job.id).state == "recommended" and lw.main() == main_before


@needs_browser
@needs_ssh
def test_an_option_the_sheet_has_no_routes_for_is_never_drawn_or_picked(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """complement (same review): an unknown option key fell back to the sign routes. A row listing one (here "zap") draws
    only the acts the sheet has routes for, and a link that picks it offers no one-tap sign."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    with rig.serving() as httpd:
        sheet = httpd.dispatch_sheet
        real_row = sheet.row

        def row(job_id: str) -> Any:
            r = real_row(job_id)
            if r is not None:
                r["options"].append({"key": "zap", "text": "Zap", "signers": list(r["signers"]), "line": "zap it"})
            return r

        sheet.row = row
        v16 = real_row(rig.job.id)["sha256"][:16]
        path = f"/sign/dispatch/{rig.job.id}"
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(),
            {"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                     f"location.href = '{path}#v={v16}&choice=zap'; return 1; }})()"},
            {"until": f"location.pathname === '{path}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "!!document.getElementById('sheet-picked')"},
            {"eval": "(document.getElementById('sheet-pick-miss') || {}).textContent || ''"},
            {"eval": "/zap it/.test(document.getElementById('sheet-body').textContent)"},
        ])
    v = out["values"]
    assert v[1] == "open" and v[2] == "sign,cancel" and v[3] is False, v
    assert "not on this item" in v[4] and v[5] is False, v
    assert out["posts"] == [] and rig.state() == "filed"


@needs_browser
@needs_ssh
def test_two_dispatch_slots_both_fill(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """codex MED / complement LOW 4: the fill sequence was module-global, so a second slot drawn while the first's read
    was out left the first empty for good. Each slot has its own."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    slots = "[...document.querySelectorAll('.dispatch-slot')]"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP,
            {"until": "!!(window.LevainNeeds && window.LevainNeeds.history)", "ms": 30000},
            # the dispatch read answers 3 s late, so the first slot's fill is still out when the second slot is drawn
            {"eval": "(() => { const f = window.fetch; window.fetch = (u, o) => (String(u).indexOf('/cockpit/dispatch.json') >= 0"
                     " ? new Promise((r) => setTimeout(r, 3000)).then(() => f(u, o)) : f(u, o)); return 1; })()"},
            {"eval": "window.LevainNeeds.history() || 1"},
            {"until": f"!!document.querySelector('#levain-modal') && {slots}.length === 1 && 1", "ms": 30000},
            # the first slot stays on the page, its fill still out; the history opened again draws a second one
            {"eval": f"(() => {{ document.body.appendChild({slots}[0]); return 1; }})()"},
            {"eval": "window.LevainNeeds.history() || 1"},
            {"until": f"{slots}.length === 2 && 1", "ms": 30000},
            {"eval": f"{slots}.map(s => !!s.querySelector('[data-dispatch-job]')).join(',')"},
            {"until": f"{slots}.every(s => s.querySelector('[data-dispatch-job]')) && 1", "ms": 12000},
        ])
    assert out["values"][-1] == 1, out["values"]


# --- fix 1029-2 L3 round 1 (deepseek, complement on 2be30145) ---------------------------------------------------------

@needs_browser
@needs_ssh
def test_a_redraw_with_the_status_line_gone_is_caught_and_keeps_the_controls_locked(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """deepseek: redraw() read #sheet-note before its promise chain, so a missing status line threw uncaught and the catch
    that locks the controls never ran. Reproduced red: one unhandled rejection and no re-read. The status line is
    removed the moment the refusal is said; the re-read then fails: no rejection escapes and no control is live."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    sign = "document.querySelector('#sheet-actions [data-choice=\"sign\"]')"
    with rig.serving() as httpd:
        _amend_at_first(httpd, rig, "challenge")
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "(() => { window.__rej = 0; window.__reads = 0; "
                     "window.addEventListener('unhandledrejection', () => { window.__rej++; }); "
                     "const f = window.fetch; window.fetch = (u, o) => { if (String(u).indexOf('/sheet.json') >= 0) "
                     "{ window.__reads++; throw new TypeError('no read'); } return f(u, o); }; "
                     "const n = document.getElementById('sheet-note'); "
                     "new MutationObserver(() => { if (/changed since it was shown/.test(n.textContent)) n.remove(); })"
                     ".observe(n, { childList: true, characterData: true, subtree: true }); return 1; })()"},
            {"eval": f"{sign}.click() || 1"},
            {"until": "(window.__reads > 0 || window.__rej > 0) && 1", "ms": 30000},
            {"eval": "[window.__rej, window.__reads, !document.getElementById('sheet-note'), "
                     "[...document.querySelectorAll('#sheet-actions button, #sheet-actions input')].every(c => c.disabled)]"},
        ])
    v = out["values"]
    assert v[-1] == [0, 1, True, True], v
    assert rig.state() == "filed"


@needs_browser
@needs_ssh
def test_a_409_that_is_not_a_change_is_not_signed_and_the_sheet_is_not_read_again(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    """complement LOW 1: every dispatch 409 was read as "the job changed" and the sheet re-read and redrew. The
    authorise's "authorise it from the laptop" is a 409 that is no change: it is a plain "Not signed" in the server's
    words, the same controls stay (unlocked), and nothing is read again. Reproduced red: the controls were redrawn."""
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    fp = rig.pk.key.fingerprint
    changes = [{"path": "src/a.py", "status": "M", "added": 1, "deleted": 1, "binary": False, "gitlink": False}]
    card = {"onto": "main", "run": 1, "tier": "T2", "changes": changes, "change_overflow": 0, "files": 1,
            "changed_lines": 2, "signers": [fp], "note": "", "problem": None, "staged": "f" * 40, "base": "e" * 40}
    auth = "document.querySelector('#sheet-actions [data-choice=\"authorise\"]')"
    with rig.serving() as httpd:
        # the card is the listing's; the store's job is filed, so the phone port refuses the authorise (409, the laptop)
        _as_recommended(httpd, rig.job, card)
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(), {"eval": GETS},
            {"eval": f"(() => {{ window.__reads = 0; {auth}.__mark = 1; const f = window.fetch; "
                     "window.fetch = (u, o) => { if (String(u).indexOf('/sheet.json') >= 0) window.__reads++; "
                     "return f(u, o); }; return 1; })()"},
            {"eval": f"{auth}.click() || 1"},
            {"until": f"/from the laptop/.test({NOTE}) && {auth} && !{auth}.disabled && {NOTE}", "ms": 30000},
            {"eval": f"[{auth}.__mark === 1, window.__reads, window.__gets, {STATE}]"},
        ])
    v = out["values"]
    assert "Not signed" in v[-2] and "nothing was signed" in v[-2] and "changed since" not in v[-2], v
    assert v[-1] == [True, 0, 0, "open"], v
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/authorise-challenge"], posts
    assert rig.state() == "filed"
