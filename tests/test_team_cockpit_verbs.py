"""The Team zone's verbs (TEAM_VIEW_PLAN T3, DESIGN_T3_VERBS.md): Fetch now (C1) and the join accept (T3, through the
broker, fired only by a laptop-presence signature over the stored record). Real git ledgers (the fixtures of
test_team_git), a real levain install inside the clone the cockpit serves, real enrolled keys. Each test was run
against the code before the change and seen to fail."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.team.transport import GitLedger, Repo
from tests.test_cockpit_k2a import _get, _install, _post, _serve
from tests.test_cockpit_k2b_broker import Key
from tests.test_team_git import KEYS, _keys, clone, git, join, record_ruling, team, two  # noqa: F401 (fixtures)

PREVIEW = {"tier_seen": "T3", "gesture_seen": "laptop-only"}


def _gl(repo: Path) -> GitLedger:
    return GitLedger(Repo.discover(repo))


@contextmanager
def _cockpit(repo: Path):
    """A reported, token-gated cockpit over a levain install inside ``repo``: its Team zone reads that clone's ledger."""
    _root, src, _ids = _install(repo)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        _get(f"{url}/cockpit/manifest.json", token="T")
        yield url, httpd


def _panel(url: str, pid: str) -> dict:
    _get(f"{url}/cockpit/manifest.json", token="T")       # the zone's panels are discovered on a manifest read
    return _get(f"{url}/cockpit/panel/{pid}.json", token="T")


def _offers(url: str, pid: str) -> list[str]:
    return [a["verb"] for a in _panel(url, pid).get("actions") or []]


def _fetch(url: str, key: str) -> tuple[int, dict]:
    return _post(f"{url}/cockpit/verb", {"verb": "team_fetch", "panel_id": "team-sync", "params": {},
                                         "idempotency_key": key}, token="T")


def _about(url: str, kind: str, handle: str) -> str:
    """The digest the Joins row shows for ``(kind, handle)``: what a real signer reads off the pane and sends."""
    (about,) = [r["facets"]["about"] for r in _panel(url, "team-joins")["rows"]
                if r["id"].startswith(f"join:{kind}:{handle}:") and "about" in r["facets"]]
    return about


def _accept(url: str, kind: str, handle: str, about: str | None = None, **env) -> tuple[int, dict]:
    about = about if about is not None else _about(url, kind, handle)
    return _post(f"{url}/cockpit/verb", {"verb": "team_join_accept", "panel_id": "team-joins",
                                         "params": {"kind": kind, "handle": handle, "about": about}, **env}, token="T")


def _sign(url: str, held: dict, key) -> dict:
    status, out = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature",
                        {"signature": key.sign(held["record"].encode())}, token="T")
    assert status == 200, out
    return out


def _settled(url: str, held: dict, out: dict, key, wait_s: float = 40.0) -> dict:
    """The pending's outcome once the broker's re-drive has run (a transient fault backs off 5 s, doubling): the same
    signature is posted again, which answers the stored state without deciding anything anew."""
    end = time.monotonic() + wait_s
    while out["state"] == "applying" and time.monotonic() < end:
        time.sleep(0.25)
        status, out = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature",
                            {"signature": key.sign(held["record"].encode())}, token="T")
    return out


def _second_machine(tmp: Path, ben: Path) -> Path:
    """Clone C: ben's second machine, joined with its own key (dan's file), which ben then proposes as his."""
    c = clone(tmp, "ben2", "ben@ex.com")
    assert join(c, "dan") != 0                 # joined and pinned, but no key of a member yet
    assert team("key", "add", "ben", str(KEYS["dan"]), repo=ben) == 0
    return c


def _keys_of(handle: str, repo: Path) -> set[str]:
    from levain.team import tenure as T
    g = _gl(repo)
    g.sync(push=False)
    g.forget_derivation()
    return T.key_fps(g.derivation().tenure, handle)


# --- Fetch now (C1) ------------------------------------------------------------------------------------------------


def test_fetch_now_brings_the_teammates_entry_and_the_zone_shows_it_next_read(two):
    tmp, ana, ben = two
    with _cockpit(ben) as (url, _httpd):
        assert "Per-line rounding stays." not in str(_panel(url, "team-canon"))   # the zone's read is cached now
        assert record_ruling(ana) == 0                                              # ana records and pushes
        assert _offers(url, "team-sync") == ["team_fetch"]
        status, out = _fetch(url, "f1")
        assert (status, out.get("ok"), out["result"]["summary"]) == (200, True, "fetched"), out
        assert "Per-line rounding stays." in {e["words"] for e in _gl(ben).ledger().in_force}
        # the cached read was made old by the fetch: the very next read shows it, not the copy from before
        assert "Per-line rounding stays." in str(_panel(url, "team-canon"))


def test_a_ledger_with_no_remote_offers_no_fetch(tmp_path):
    git("init", "-q", "--initial-branch=main", "solo", cwd=tmp_path)
    solo = tmp_path / "solo"
    git("config", "user.email", "ana@ex.com", cwd=solo)
    git("config", "user.name", "ana", cwd=solo)
    (solo / "a.txt").write_text("a")
    git("add", ".", cwd=solo)
    git("commit", "-qm", "init", cwd=solo)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--signing-key",
                str(KEYS["ana"]), "--no-install", repo=solo) == 0
    with _cockpit(solo) as (url, _httpd):
        assert "no remote" in str(_panel(url, "team-sync")) and _offers(url, "team-sync") == []
        status, out = _fetch(url, "f1")
        assert (status, out["error"]) == (404, "not_offered"), out
    # the fire itself, reached past a stale offer: no remote is a refusal, never a 200 "local only" that fetched nothing
    from levain.team.cockpit_verbs import _fetch_fire
    from levain.writes import EditError
    with pytest.raises(EditError) as ei:
        _fetch_fire(solo)(None, {}, {}, False)
    assert (ei.value.code, ei.value.http_status) == ("refused", 409) and "no remote" in str(ei.value), ei.value


# --- the join accept (T3) ------------------------------------------------------------------------------------------


def test_a_key_waiting_on_this_machine_is_accepted_only_by_a_laptop_presence_signature(two, tmp_path):
    tmp, ana, ben = two
    c = _second_machine(tmp, ben)
    with _cockpit(c) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        phone = Key(tmp_path, "phone").enroll(httpd.broker, "display-phone")
        assert _fetch(url, "f1")[0] == 200                       # C sees ben's proposal of its key
        joins = _panel(url, "team-joins")
        assert "this machine's key is proposed for ben" in str(joins) and "levain team key confirm" in str(joins)
        assert _offers(url, "team-joins") == ["team_join_accept"]
        status, pre = _accept(url, "key", "ben", dry_run=True)
        assert (status, pre["tier"], pre["gesture"]) == (200, "T3", "laptop-only"), pre
        status, out = _accept(url, "key", "ben", intent="reported", idempotency_key="r1", confirm=False, **PREVIEW)
        assert status == 403 and "laptop-presence" in out["message"], out          # the token alone never fires a T3
        status, held = _accept(url, "key", "ben")
        assert (status, held["state"], held["tier"], held["intent"]) == (200, "WAITING", "T3", "sign"), held
        status, refused = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature",
                                {"signature": phone.sign(held["record"].encode())}, token="T")
        assert (status, refused["state"]) == (200, "REFUSED"), refused                # a display-phone key: T2 at most
        assert _gl(c).own_fingerprint() not in _keys_of("ben", ben)
        status, held = _accept(url, "key", "ben")
        sig = lp.sign(held["record"].encode())
        status, done = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature", {"signature": sig}, token="T")
        assert (status, done["state"]) == (200, "COMMITTED"), done
        assert _gl(c).own_fingerprint() in _keys_of("ben", ben)               # in force, and pushed: ben's clone sees it
        status, again = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature", {"signature": sig}, token="T")
        assert (status, again["state"], again["replay"]) == (200, "COMMITTED", True), again
        assert _offers(url, "team-joins") == []                               # nothing waits on this machine now


def test_a_key_removed_before_the_signature_is_not_applied_and_nothing_is_committed(two, tmp_path):
    tmp, ana, ben = two
    c = _second_machine(tmp, ben)
    with _cockpit(c) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        status, held = _accept(url, "key", "ben")
        assert held["state"] == "WAITING", held
        fp = _gl(c).own_fingerprint()
        assert team("key", "remove", "ben", fp, repo=ben) == 0               # ben withdraws it from his first machine
        status, out = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature",
                             {"signature": lp.sign(held["record"].encode())}, token="T")
        # decided, then refused by the store at apply: the signature is spent, nothing is in force
        assert (status, out["state"]) == (200, "COMMITTED_INCOMPLETE") and out["reason"].endswith(": refused"), out
        g = _gl(c)
        g.forget_derivation()
        assert fp not in _keys_of("ben", ben) and fp not in str(g.derivation().tenure.pending_keys)
        # C's ledger tip is the remote's (the removal it fetched): C committed nothing of its own
        assert g.head() == git("ls-remote", str(tmp / "origin.git"), g.ref, cwd=c).split()[0]


def test_an_owner_offer_is_accepted_through_the_signed_t3_and_a_re_offer_refuses_the_old_one(two, tmp_path):
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    with _cockpit(ben) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        assert "levain team accept" in str(_panel(url, "team-joins"))
        status, stale = _accept(url, "owner-offer", "ben")
        assert stale["state"] == "WAITING", stale
        assert team("owner", "ana", repo=ana) == 0                            # re-offered to another handle
        status, out = _post(f"{url}/cockpit/pending/{stale['pending_id']}/signature",
                             {"signature": lp.sign(stale["record"].encode())}, token="T")
        # decided, then refused by the store at apply: the signature is spent, nothing is in force
        assert (status, out["state"]) == (200, "COMMITTED_INCOMPLETE") and out["reason"].endswith(": refused"), out
        g = _gl(ben)
        g.forget_derivation()
        assert g.derivation().team.owner == "ana"
        assert team("owner", "ben", repo=ana) == 0                            # offered to ben again
        assert _fetch(url, "f2")[0] == 200
        status, held = _accept(url, "owner-offer", "ben")
        status, done = _post(f"{url}/cockpit/pending/{held['pending_id']}/signature",
                             {"signature": lp.sign(held["record"].encode())}, token="T")
        assert (status, done["state"]) == (200, "COMMITTED"), done
    g = _gl(ana)
    g.sync(push=False)
    g.forget_derivation()
    d = g.derivation()
    assert (d.team.owner, d.tenure.offer) == ("ben", None)                    # published: ana's clone sees it


def test_the_accept_refuses_params_outside_kind_and_a_valid_handle_at_issue(two, tmp_path):
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    with _cockpit(ben) as (url, _httpd):
        assert _fetch(url, "f1")[0] == 200
        about = _about(url, "owner-offer", "ben")
        for params, code in (({"kind": "owner", "handle": "ben", "about": about}, "refused"),
                             ({"kind": "key", "handle": "pack-ben", "about": about}, "refused"),
                             ({"kind": "key", "handle": "../ben", "about": about}, "refused"),
                             ({"kind": "owner-offer", "handle": "ben"}, "refused"),                  # no digest
                             ({"kind": "owner-offer", "handle": "ben", "about": about.upper()}, "refused"),
                             ({"kind": "owner-offer", "handle": "ben", "about": about[:-1]}, "refused"),
                             ({"kind": "key", "handle": "ben", "about": about, "fingerprint": "SHA256:x"},
                              "field_not_allowed")):
            status, out = _post(f"{url}/cockpit/verb", {"verb": "team_join_accept", "panel_id": "team-joins",
                                                        "params": params}, token="T")
            assert (status, out["error"]) == (409 if code == "refused" else 400, code), (params, out)


def test_a_callers_verb_under_a_team_verb_name_is_refused_at_construction(tmp_path):
    from levain.web_server import make_server
    from levain.writes import ActionVerb

    _root, src, _ids = _install(tmp_path)
    with pytest.raises(ValueError, match="Team zone"):
        make_server(src, host="127.0.0.1", port=0, install_class="reported",
                    extra_verbs={"team_fetch": ActionVerb(handler=lambda p: {}, label="mine")})


# --- r1 fixes (T3_r1_findings.md FIX 1-4) --------------------------------------------------------------------------


def test_a_push_that_fails_is_not_applied_and_the_re_drive_publishes_it(two, tmp_path):
    """FIX 1: the act is committed here but the push fails (a real push: the remote's pushurl names a path that does
    not exist, the fetch url still works). Not APPLIED: the pending stays applying, and once the push can land the
    broker's re-drive finds the act done here, pushes it and only then records it."""
    tmp, ana, ben = two
    c = _second_machine(tmp, ben)
    with _cockpit(c) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        status, held = _accept(url, "key", "ben")
        assert held["state"] == "WAITING", held
        remote = _gl(c).remote
        git("config", f"remote.{remote}.pushurl", str(tmp / "gone.git"), cwd=c)
        out = _sign(url, held, lp)
        assert out["state"] == "applying", out                                # not COMMITTED: nothing published
        fp = _gl(c).own_fingerprint()
        g = _gl(c)
        g.forget_derivation()
        assert fp in T_key_fps(g, "ben") and fp not in _keys_of("ben", ben)   # in force here only
        git("config", "--unset", f"remote.{remote}.pushurl", cwd=c)
        out = _settled(url, held, out, lp)
        assert out["state"] == "COMMITTED", out
        assert fp in _keys_of("ben", ben)                                     # published by the re-drive


def T_key_fps(g: GitLedger, handle: str) -> set[str]:
    from levain.team import tenure as T
    return T.key_fps(g.derivation().tenure, handle)


def test_a_busy_lock_during_the_act_is_transient_and_the_re_drive_applies_it(two, tmp_path, monkeypatch):
    """FIX 2: the act meets the worktree lock held by another thread (a real flock, a real TeamBusy; the act's wait is
    cut to 0.3 s so the test need not hold it past the act's 30 s). Busy is not a refusal: the signature is not spent,
    and the re-drive applies it once the lock is free."""
    tmp, ana, ben = two
    c = _second_machine(tmp, ben)
    real = GitLedger.confirm_own_key
    calls = []

    def busy_once(self, d, **kw):
        calls.append(1)
        if len(calls) == 1:
            got, free = threading.Event(), threading.Event()

            def hold() -> None:
                with GitLedger(Repo.discover(c)).lock(timeout=5):
                    got.set()
                    free.wait(10)
            t = threading.Thread(target=hold, daemon=True)
            t.start()
            assert got.wait(5)
            try:
                with self.lock(timeout=0.3):
                    pass
            finally:
                free.set()
                t.join(5)
        return real(self, d, **kw)
    monkeypatch.setattr(GitLedger, "confirm_own_key", busy_once)
    with _cockpit(c) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        status, held = _accept(url, "key", "ben")
        out = _sign(url, held, lp)
        assert out["state"] == "applying", out                                # not REFUSED, not spent
        out = _settled(url, held, out, lp)
        assert (out["state"], len(calls)) == ("COMMITTED", 2), out
        assert _gl(c).own_fingerprint() in _keys_of("ben", ben)


def test_a_re_offer_to_the_same_handle_with_another_email_refuses_what_was_signed(two, tmp_path):
    """FIX 3: the signature binds the offer itself (``about``), not only its handle: re-offered to ben with another
    address between the issue and the signature, the accept is refused and team.toml keeps ben's address."""
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    with _cockpit(ben) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        signed = _about(url, "owner-offer", "ben")
        status, held = _accept(url, "owner-offer", "ben", signed)
        assert held["state"] == "WAITING", held
        assert team("owner", "ben", "--email", "mallory@ex.com", repo=ana) == 0
        out = _sign(url, held, lp)
        assert out["state"] == "COMMITTED_INCOMPLETE" and out["reason"].endswith(": refused"), out
        g = _gl(ben)
        g.forget_derivation()
        d = g.derivation()
        assert d.team.owner == "ana" and d.team.members["ben"] == "ben@ex.com", (d.team.owner, d.team.members)
        assert _fetch(url, "f2")[0] == 200
        assert _about(url, "owner-offer", "ben") != signed            # the pane now shows the new offer's digest


def test_a_confirm_that_did_nothing_on_a_clone_with_no_remote_is_not_applied(two, tmp_path, monkeypatch):
    """FIX 4: with no remote the act is re-checked too: a ``confirm_own_key`` that returns False (nothing moved) is a
    refusal, never an APPLIED record of an act that is not in force."""
    tmp, ana, ben = two
    c = _second_machine(tmp, ben)
    monkeypatch.setattr(GitLedger, "confirm_own_key", lambda self, d, **kw: False)
    with _cockpit(c) as (url, httpd):
        lp = Key(tmp_path, "lp").enroll(httpd.broker, "laptop-presence")
        assert _fetch(url, "f1")[0] == 200
        status, held = _accept(url, "key", "ben")
        _gl(c).save_state(remote="")                                          # this clone is local only from now
        assert _gl(c).remote is None
        out = _sign(url, held, lp)
        assert out["state"] == "COMMITTED_INCOMPLETE" and out["reason"].endswith(": refused"), out
        g = _gl(c)
        g.forget_derivation()
        assert g.own_fingerprint() not in T_key_fps(g, "ben")
