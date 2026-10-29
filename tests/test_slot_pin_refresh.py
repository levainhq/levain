"""PIN_REFRESH (PIN_REFRESH_DESIGN §6 T1-T4): the identity child refreshes the stored login once, writes it back, and
asks the profile on the new access token; root judges the line. The real :mod:`levain.slot.identity_child` runs
in-process against a real credential file in the session tree, with a fake token server and profile behind
``fetch(..., opener=)``; the rest is the g3 gate harness. Nothing here reaches the vendor's servers.
"""
from __future__ import annotations

import io
import json
import os
import socket
import urllib.error

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import identity_child as IC
from levain.slot import start as SS
from tests.test_slot_g3_gate import E0, OTHER, PASS, UUID, _gated, _keep, _tree, env, _la  # noqa: F401
from tests.test_slot_s2f3_start import _blob, _rendered
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

FUTURE = 4_102_444_800_000        # a live access token's expiresAt (ms)
PAST = 1_761_700_000_000          # the 10-28 state: expired


def _cred(access="acc-A", refresh="rt-A", expires=PAST) -> bytes:
    return json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh, "expiresAt": expires,
        "scopes": ["user:inference", "user:profile"], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"},
        "other": {"kept": [1, 2]}}, separators=(",", ":")).encode()


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, body

    def read(self, n=-1):
        return self._b if n < 0 else self._b[:n]

    def close(self):
        pass


class Server:
    """A fake token endpoint and profile. ``tokens``: refresh token -> (new access, new refresh or None);
    ``profiles``: access token -> account uuid (an unknown one is a 401). ``refresh_fail`` replaces the token
    endpoint's answer: ``(status, body)`` or an exception. Like the real server (measured 10-29), a refresh that
    returns a new refresh token INVALIDATES the one it was sent: a second use is ``invalid_grant``."""

    def __init__(self, tokens, profiles, refresh_fail=None):
        self.tokens, self.profiles, self.refresh_fail = dict(tokens), profiles, refresh_fail
        self.refreshes = []
        self.profile_calls = []

    def __call__(self, env):
        return self

    def _err(self, url, code, body):
        return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))

    def open(self, req, timeout=None):
        if req.full_url == "https://platform.claude.com/v1/oauth/token":
            assert req.get_method() == "POST" and req.headers["Content-type"] == "application/json"
            body = json.loads(req.data)
            self.refreshes.append(body)
            f = self.refresh_fail
            if isinstance(f, BaseException):
                raise f
            if f is not None:
                if f[0] == 200:
                    return _Resp(200, f[1])
                raise self._err(req.full_url, f[0], f[1])
            if body["refresh_token"] not in self.tokens:
                raise self._err(req.full_url, 400, b'{"error":"invalid_grant"}')
            acc, rt = self.tokens[body["refresh_token"]]
            ans = {"access_token": acc, "expires_in": 28800}
            if rt is not None:
                ans["refresh_token"] = rt
                del self.tokens[body["refresh_token"]]          # rotation: the old one is dead
                self.tokens.setdefault(rt, (acc + "+", rt + "+"))
            return _Resp(200, json.dumps(ans).encode())
        assert req.full_url == "https://api.anthropic.com/api/oauth/profile"
        auth = req.headers["Authorization"]
        self.profile_calls.append(auth)
        uuid = self.profiles.get(auth[len("Bearer "):])
        if uuid is None:
            raise self._err(req.full_url, 401, b"")
        return _Resp(200, json.dumps({"account": {"uuid": uuid}}).encode())


def _run_child(w, srv, after=None):
    """The identity child as the harness's capture: the real ``fetch`` on the session tree's credential file.
    ``after(config_dir)`` runs once the child answered, before root reads its line."""
    real = w.seams.capture

    def capture(argv, spec_json, cgroup, bound):
        if canon.parse(spec_json)["target"] != "identity":
            return real(argv, spec_json, cgroup, bound)
        s = w.session
        env_ = {"CLAUDE_CONFIG_DIR": str(_tree(w, s).parent), "HTTPS_PROXY": "http://127.0.0.1:1"}
        ans = IC.fetch(env_, opener=srv)
        if after is not None:
            after(_tree(w, s).parent)
        return (0 if "status" in ans else 1), json.dumps(ans, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    w.seams.capture = capture


def _session(w, la, cred):
    s = _gated(w, la)
    _keep(w).write_bytes(cred)
    w.session = s
    return s


def _oauth(path):
    return json.loads(path.read_bytes())["claudeAiOauth"]


# --- T1: the swap (PIN_DESIGN §0) --------------------------------------------------------------------------------------

def test_t1_the_swap_a_valid_access_token_of_a_beside_the_refresh_token_of_b_is_refused(env):
    """A's access token is still valid; the refresh token is B's. A profile call on A's token names A (the old gate
    passed this file); the refresh turns B's refresh token into B's access token, so the profile names B."""
    w, la, _ = env
    s = _session(w, la, _cred(access="acc-A", refresh="rt-B", expires=FUTURE))
    srv = Server({"rt-B": ("acc-B", "rt-B2")}, {"acc-A": UUID, "acc-B": OTHER})
    _run_child(w, srv)
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    assert len(srv.refreshes) == 1 and srv.profile_calls == ["Bearer acc-B"]
    assert w.state(s).fields["identity"] == {"observed_id": OTHER, "result": "identity-changed"}
    assert len(la.of("vendor")) == 0 and len(la.of("clone")) == 0
    assert not _tree(w, s).exists()
    assert _oauth(_keep(w))["refreshToken"] == "rt-B2" and _oauth(_keep(w))["accessToken"] == "acc-B"


# --- T2 / T3: the 10-28 expired state, and the rotation written back ---------------------------------------------------

def test_t2_t3_an_expired_access_token_refreshes_matches_and_the_rotation_is_written_back(env):
    w, la, _ = env
    s = _session(w, la, _cred(access="acc-old", refresh="rt-A", expires=PAST))
    before = json.loads(_keep(w).read_bytes())
    srv = Server({"rt-A": ("acc-new", "rt-A2")}, {"acc-new": UUID})      # the old access token is a 401
    _run_child(w, srv)
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert len(la.of("vendor")) == 1 and len(srv.refreshes) == 1
    assert srv.refreshes[0]["grant_type"] == "refresh_token" and srv.refreshes[0]["client_id"] == IC.CLIENT_ID
    assert srv.refreshes[0]["scope"] == "user:inference user:profile"
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "matched"}
    # T3: the file the vendor starts on holds the new tokens, a ms expiry, every other key as it was
    t = _tree(w, s)
    after = json.loads(t.read_bytes())
    assert after["claudeAiOauth"]["accessToken"] == "acc-new" and after["claudeAiOauth"]["refreshToken"] == "rt-A2"
    assert after["claudeAiOauth"]["expiresAt"] > 1_700_000_000_000 * 1.0 and after["claudeAiOauth"]["expiresAt"] != PAST
    for k in ("scopes", "subscriptionType", "rateLimitTier"):
        assert after["claudeAiOauth"][k] == before["claudeAiOauth"][k]
    assert after["other"] == before["other"]
    assert not (t.parent / IC.TEMP_NAME).exists()
    st = os.stat(t)
    assert st.st_nlink == 1 and st.st_uid == os.getuid() and (st.st_mode & 0o777) == 0o600


def _boom(*a, **k):
    raise OSError(5, "injected")


def test_a_a_rename_that_fails_after_a_rotating_200_keeps_the_new_login_and_the_retry_works(env, monkeypatch):
    """L3 r1 HIGH (codex + deepseek): the server rotated rt-A to rt-A2 (rt-A is dead), then every rename of the written
    temp failed. On 876694f6 the temp (the only copy of rt-A2) was unlinked and keep/ got rt-A back, so the retry
    answered invalid_grant: a lost login. Now the temp is kept, root moves it into place before the park, keep/ holds
    rt-A2, and the retry of the same session refreshes rt-A2 and matches."""
    w, la, _ = env
    s = _session(w, la, _cred(access="acc-old", refresh="rt-A"))
    srv = Server({"rt-A": ("acc-new", "rt-A2")}, {"acc-new": UUID, "acc-new+": UUID})
    _run_child(w, srv)
    real = IC._replace
    monkeypatch.setattr(IC, "_replace", _boom)
    assert w.main(["start", s])[1]["refused"] == "identity-unreachable"
    monkeypatch.setattr(IC, "_replace", real)       # never monkeypatch.undo(): it drops the env fixture's patches
    assert w.state(s).fields["identity"] == {"result": "identity-unreachable"}
    assert srv.profile_calls == [] and len(la.of("vendor")) == 0
    assert _oauth(_keep(w))["refreshToken"] == "rt-A2" and not _tree(w, s).exists()
    assert not (_tree(w, s).parent / IC.TEMP_NAME).exists()
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert [r["refresh_token"] for r in srv.refreshes] == ["rt-A", "rt-A2"] and len(la.of("vendor")) == 1
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "matched"}


def test_a_a_kept_temp_root_cannot_promote_is_named_expired_not_try_again(env, monkeypatch):
    """The promote's own refusal: the kept temp is not the child's shape (here 0644), so root leaves it and the stored
    login is the dead one: identity-expired, the sign-in line, never 'the stored login is kept'."""
    w, la, _ = env
    s = _session(w, la, _cred(access="acc-old", refresh="rt-A"))
    srv = Server({"rt-A": ("acc-new", "rt-A2")}, {"acc-new": UUID})
    _run_child(w, srv, after=lambda cfg: os.chmod(cfg / IC.TEMP_NAME, 0o644))
    monkeypatch.setattr(IC, "_replace", _boom)
    assert w.main(["start", s])[1]["refused"] == "identity-expired"
    assert w.state(s).fields["identity"] == {"result": "identity-expired"}
    assert _oauth(_keep(w))["refreshToken"] == "rt-A" and len(la.of("vendor")) == 0


@pytest.mark.parametrize("rotates,name", [(True, "identity-expired"), (False, "identity-unreachable")])
def test_a_a_write_that_fails_after_the_200_unlinks_the_partial_temp_and_names_the_loss(env, monkeypatch, rotates,
                                                                                      name):
    """A write that fails part way leaves no whole copy: the temp is unlinked. With a rotated token the login is gone
    (identity-expired, a sign-in repairs it); with none returned the old one still works (identity-unreachable)."""
    w, la, _ = env
    original = _cred(access="acc-old", refresh="rt-A")
    s = _session(w, la, original)
    srv = Server({"rt-A": ("acc-new", "rt-A2" if rotates else None)}, {"acc-new": UUID})
    _run_child(w, srv)
    monkeypatch.setattr(IC, "_write", _boom)
    assert w.main(["start", s])[1]["refused"] == name
    assert _keep(w).read_bytes() == original and not (_tree(w, s).parent / IC.TEMP_NAME).exists()


# --- T4: the failure table (PIN_REFRESH_DESIGN §4): one refresh, never a retry -------------------------------------------

HTML = b"<html>Just a moment...</html>"
CASES = [
    ("invalid_grant 400", (400, b'{"error":"invalid_grant"}'), "identity-expired"),
    ("invalid_grant 401", (401, b'{"error":"invalid_grant"}'), "identity-expired"),
    # E (codex MED 4): a client fault is not a dead login; a sign-in with the same client cannot repair it
    ("invalid_client 401", (401, b'{"error":"invalid_client"}'), "identity-unreachable"),
    ("unauthorized_client 400", (400, b'{"error":"unauthorized_client"}'), "identity-unreachable"),
    ("oauth 403", (403, b'{"error":"access_denied"}'), "identity-unreachable"),
    ("other 400 word", (400, b'{"error":"invalid_request"}'), "identity-unreachable"),
    ("html 403", (403, HTML), "identity-unreachable"),
    ("429", (429, b'{"error":"rate_limited"}'), "identity-unreachable"),
    ("503", (503, HTML), "identity-unreachable"),
    ("timeout", socket.timeout("timed out"), "identity-unreachable"),
    ("no access_token", (200, b'{"expires_in":10}'), "identity-unreachable"),
    ("unparsable 200", (200, b"not json"), "identity-unreachable"),
    # B (codex + complement + deepseek): 1e309 parses as infinity; int() of it raised OverflowError outside the catch
    ("expires_in 1e309", (200, b'{"access_token":"acc-n","expires_in":1e309}'), "identity-unreachable"),
    ("expires_in Infinity", (200, b'{"access_token":"acc-n","expires_in":Infinity}'), "identity-unreachable"),
    ("expires_in over a year", (200, b'{"access_token":"acc-n","expires_in":99999999}'), "identity-unreachable"),
    ("token too long", (200, b'{"access_token":"' + b"a" * 5000 + b'","expires_in":10}'), "identity-unreachable"),
]


@pytest.mark.parametrize("label,fail,name", CASES, ids=[c[0] for c in CASES])
def test_t4_each_refresh_failure_has_its_name_one_attempt_and_the_login_parked_back(env, label, fail, name):
    w, la, _ = env
    original = _cred()
    s = _session(w, la, original)
    srv = Server({}, {}, refresh_fail=fail)
    _run_child(w, srv)
    assert w.main(["start", s])[1]["refused"] == name
    assert len(srv.refreshes) == 1 and srv.profile_calls == []
    assert w.state(s).fields["identity"] == {"result": name}
    assert len(la.of("vendor")) == 0 and len(la.of("clone")) == 0
    assert _keep(w).read_bytes() == original and not _tree(w, s).exists()


def test_t4_a_refreshed_token_the_profile_refuses_is_expired(env):
    w, la, _ = env
    s = _session(w, la, _cred())
    srv = Server({"rt-A": ("acc-new", "rt-A2")}, {})          # the new access token is a 401
    _run_child(w, srv)
    assert w.main(["start", s])[1]["refused"] == "identity-expired"
    assert len(srv.refreshes) == 1 and _oauth(_keep(w))["refreshToken"] == "rt-A2"


# --- the child and the parser, line by line -----------------------------------------------------------------------------

def test_the_refresh_goes_to_the_token_origin_and_the_bearer_to_the_api_origin_only(tmp_path):
    (tmp_path / ".credentials.json").write_bytes(_cred())
    env_ = {"CLAUDE_CONFIG_DIR": str(tmp_path), "HTTPS_PROXY": "http://127.0.0.1:1"}
    assert IC.TOKEN_ORIGIN == ("https", "platform.claude.com", 443) and IC._origin(IC.TOKEN_URL) == IC.TOKEN_ORIGIN
    assert IC.fetch(env_, token_url="https://evil.example/v1/oauth/token") == {"error": "bad-origin"}
    assert IC.fetch(env_, url="https://evil.example/api/oauth/profile") == {"error": "bad-origin"}
    assert (tmp_path / ".credentials.json").read_bytes() == _cred()


def test_a_leftover_temp_refuses_before_the_token_is_spent(tmp_path):
    (tmp_path / ".credentials.json").write_bytes(_cred())
    (tmp_path / IC.TEMP_NAME).write_bytes(b"x")
    srv = Server({"rt-A": ("a", "b")}, {})
    ans = IC.fetch({"CLAUDE_CONFIG_DIR": str(tmp_path), "HTTPS_PROXY": "http://x:1"}, opener=srv)
    assert ans == {"refresh": "write-failed"} and srv.refreshes == []


def _env(tmp_path):
    return {"CLAUDE_CONFIG_DIR": str(tmp_path), "HTTPS_PROXY": "http://x:1"}


def test_c_a_login_too_large_to_write_back_is_refused_before_the_refresh_is_sent(tmp_path):
    """C (codex MED 5): a file near END's 64 KiB cap could be rewritten over it and stranded (return_move refuses
    keep-shape). The worst-case write-back is checked before the POST: nothing is spent, nothing written."""
    obj = json.loads(_cred())
    obj["pad"] = "x" * (IC.CRED_MAX - len(_cred()) - 200)        # fits today; the max-length tokens would not
    raw = json.dumps(obj, separators=(",", ":")).encode()
    assert len(raw) <= IC.CRED_MAX
    (tmp_path / ".credentials.json").write_bytes(raw)
    srv = Server({"rt-A": ("a", "b")}, {})
    assert IC.fetch(_env(tmp_path), opener=srv) == {"refresh": "cred-too-large"} and srv.refreshes == []
    assert (tmp_path / ".credentials.json").read_bytes() == raw and not (tmp_path / IC.TEMP_NAME).exists()
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(1, b'{"refresh":"cred-too-large"}')
    assert e.value.name == "identity-unreachable"


@pytest.mark.parametrize("stored,sent", [("0d9b1c6e-1a2b-4c3d-8e9f-0123456789ab", "0d9b1c6e-1a2b-4c3d-8e9f-0123456789ab"),
                                         ("not-a-uuid", IC.CLIENT_ID), (None, IC.CLIENT_ID)])
def test_d_the_stored_client_id_is_sent_when_it_is_a_uuid(tmp_path, stored, sent):
    """D (codex MED 2; Claude Code 2.1.312: ``client_id:s??on().CLIENT_ID`` with ``clientId:ie.clientId``)."""
    obj = json.loads(_cred())
    if stored is not None:
        obj["claudeAiOauth"]["clientId"] = stored
    (tmp_path / ".credentials.json").write_bytes(json.dumps(obj).encode())
    srv = Server({"rt-A": ("acc-n", "rt-n")}, {"acc-n": UUID})
    assert IC.fetch(_env(tmp_path), opener=srv)["refresh"] == 200
    assert srv.refreshes[0]["client_id"] == sent
    if stored is not None:                                        # kept in the file, as every other key
        assert json.loads((tmp_path / ".credentials.json").read_bytes())["claudeAiOauth"]["clientId"] == stored


def test_f_a_login_with_no_refresh_token_is_expired_and_nothing_is_sent(env):
    """F (complement + deepseek): it failed closed as identity-unparsable; the name now says a sign-in repairs it."""
    w, la, _ = env
    obj = json.loads(_cred(expires=FUTURE))
    del obj["claudeAiOauth"]["refreshToken"]
    s = _session(w, la, json.dumps(obj).encode())
    srv = Server({}, {"acc-A": UUID})
    _run_child(w, srv)
    assert w.main(["start", s])[1]["refused"] == "identity-expired"
    assert srv.refreshes == [] and srv.profile_calls == [] and len(la.of("vendor")) == 0


def test_g_a_claude_answer_with_no_refresh_is_unparsable():
    """G (complement LOW 5): a profile-only line (a path that skipped the refresh) never passes the Claude gate."""
    line = b'{"status":200,"body":"{\\"account\\":{\\"uuid\\":\\"' + UUID.encode() + b'\\"}}"}'
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(0, line)
    assert e.value.name == "identity-unparsable"
    assert ID.parse_profile_answer(0, line, refreshed=False) == UUID


def test_h_roots_capture_bound_covers_both_requests_and_the_rename_retries():
    """H (complement LOW 6): root kills the child at CHECK_S; the token POST, the profile GET and the rename retries
    must fit well inside it, or a kill could land after the rotation."""
    from levain.slot import runtime_helper as H
    assert 2 * IC.TIMEOUT_S + 0.05 * IC.RENAME_TRIES * (IC.RENAME_TRIES + 1) < H.CHECK_S


def test_no_token_appears_in_the_child_line(tmp_path):
    (tmp_path / ".credentials.json").write_bytes(_cred())
    srv = Server({"rt-A": ("acc-SECRET", "rt-SECRET2")}, {"acc-SECRET": UUID})
    ans = IC.fetch({"CLAUDE_CONFIG_DIR": str(tmp_path), "HTTPS_PROXY": "http://x:1"}, opener=srv)
    line = json.dumps(ans)
    assert "SECRET" not in line and "rt-A" not in line and "acc-" not in line and ans["refresh"] == 200


@pytest.mark.parametrize("out,name", [
    (b'{"refresh":200,"status":200,"body":"{\\"account\\":{\\"uuid\\":\\"' + UUID.encode() + b'\\"}}"}', None),
    (b'{"refresh":"request-failed"}', "identity-unreachable"),
    (b'{"refresh":"write-failed"}', "identity-unreachable"),
    (b'{"refresh":400,"oauth_error":"invalid_grant"}', "identity-expired"),
    (b'{"refresh":403}', "identity-unreachable"),
    (b'{"refresh":401,"oauth_error":"invalid_client"}', "identity-unreachable"),
    (b'{"refresh":403,"oauth_error":"invalid_grant"}', "identity-unreachable"),
    (b'{"refresh":"no-refresh-token"}', "identity-expired"),
    (b'{"refresh":"lost-after-spend"}', "identity-expired"),
    (b'{"refresh":"kept-after-spend"}', "identity-unreachable"),
    (b'{"refresh":500,"oauth_error":"invalid_grant"}', "identity-unreachable"),
    (b'{"refresh":200,"error":"request-failed"}', "identity-unparsable"),
    (b'{"refresh":true}', "identity-unparsable"),
    (b'{"refresh":"Bad Name!"}', "identity-unparsable"),
    (b'{"refresh":200,"status":401,"body":""}', "identity-expired"),
])
def test_the_line_parser_follows_the_table(out, name):
    code = 0 if b'"status"' in out else 1
    if name is None:
        assert ID.parse_profile_answer(code, out) == UUID
        return
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(code, out)
    assert e.value.name == name


def test_unreachable_is_a_distinct_name_from_expired():
    assert ID.UNREACHABLE == "identity-unreachable" and ID.UNREACHABLE != ID.EXPIRED
