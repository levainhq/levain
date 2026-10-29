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
    endpoint's answer: ``(status, body)`` or an exception."""

    def __init__(self, tokens, profiles, refresh_fail=None):
        self.tokens, self.profiles, self.refresh_fail = tokens, profiles, refresh_fail
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
            return _Resp(200, json.dumps(ans).encode())
        assert req.full_url == "https://api.anthropic.com/api/oauth/profile"
        auth = req.headers["Authorization"]
        self.profile_calls.append(auth)
        uuid = self.profiles.get(auth[len("Bearer "):])
        if uuid is None:
            raise self._err(req.full_url, 401, b"")
        return _Resp(200, json.dumps({"account": {"uuid": uuid}}).encode())


def _run_child(w, srv):
    """The identity child as the harness's capture: the real ``fetch`` on the session tree's credential file."""
    real = w.seams.capture

    def capture(argv, spec_json, cgroup, bound):
        if canon.parse(spec_json)["target"] != "identity":
            return real(argv, spec_json, cgroup, bound)
        s = w.session
        env_ = {"CLAUDE_CONFIG_DIR": str(_tree(w, s).parent), "HTTPS_PROXY": "http://127.0.0.1:1"}
        ans = IC.fetch(env_, opener=srv)
        return (0 if "status" in ans else 1), json.dumps(ans, sort_keys=True).encode() + b"\n"
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


def test_t3_a_fault_between_the_200_and_the_rename_leaves_no_temp_and_refuses_unreachable(env, monkeypatch):
    w, la, _ = env
    original = _cred(access="acc-old", refresh="rt-A")
    s = _session(w, la, original)
    srv = Server({"rt-A": ("acc-new", "rt-A2")}, {"acc-new": UUID})
    _run_child(w, srv)

    def boom(src, dst):
        raise OSError(5, "injected")
    monkeypatch.setattr(IC, "_replace", boom)
    assert w.main(["start", s])[1]["refused"] == "identity-unreachable"
    monkeypatch.undo()
    assert w.state(s).fields["identity"] == {"result": "identity-unreachable"}
    assert srv.profile_calls == [] and len(la.of("vendor")) == 0
    assert _keep(w).read_bytes() == original and not _tree(w, s).exists()
    assert not any(p.name == IC.TEMP_NAME for p in _keep(w).parent.iterdir())


# --- T4: the failure table (PIN_REFRESH_DESIGN §4): one refresh, never a retry -------------------------------------------

HTML = b"<html>Just a moment...</html>"
CASES = [
    ("invalid_grant 400", (400, b'{"error":"invalid_grant"}'), "identity-expired"),
    ("oauth 401", (401, b'{"error":"invalid_client"}'), "identity-expired"),
    ("oauth 403", (403, b'{"error":"access_denied"}'), "identity-expired"),
    ("other 400 word", (400, b'{"error":"invalid_request"}'), "identity-unreachable"),
    ("html 403", (403, HTML), "identity-unreachable"),
    ("429", (429, b'{"error":"rate_limited"}'), "identity-unreachable"),
    ("503", (503, HTML), "identity-unreachable"),
    ("timeout", socket.timeout("timed out"), "identity-unreachable"),
    ("no access_token", (200, b'{"expires_in":10}'), "identity-unreachable"),
    ("unparsable 200", (200, b"not json"), "identity-unreachable"),
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
