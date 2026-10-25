"""CH-D2 S9: K1's real-client corpus (R-5, R15-3, R15-12-B2 / B10): requests made by real clients (curl, git, a Python
`requests` call, pip, npm, uv) through the real glue and decider to a recording origin carry credentials; the origin
sees none, and the attempt rows name each one that was stripped. The lists are pinned by it: a name added to or removed
from `CRED_STRIP_HEADERS`, `CRED_STRIP_QUERY`, `SIGNED_URL_READ_EXEMPT` or the pagination set is a red here until this
file's pinned copy moves with it.

A client that is not installed on this machine is a skip naming it, never a stand-in. curl, git and `requests` carry
every listed header name; pip, npm and uv carry the one credential each emits on its own (basic auth from the index URL,
an `_authToken`), because they have no flag for a header of the caller's choosing. [judged by lane-s9, 10-25, against
each client's documented options]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from levain.chat_egress import classify, strip

pytestmark = pytest.mark.chat_egress_srt

# --- the pinned lists (a change re-signs, R15-3) ----------------------------------------------------------------
PINNED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-goog-api-key",
    "ocp-apim-subscription-key", "private-token", "job-token", "x-auth-token", "x-access-token", "x-amz-security-token"})
PINNED_QUERY = frozenset({"access_token", "api_key", "apikey", "key", "token", "auth", "private_token", "client_secret",
                          "password"})
PINNED_SIGNED = frozenset({
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-amz-date", "x-amz-expires", "x-amz-signedheaders",
    "x-goog-signature", "x-goog-credential", "x-goog-date", "x-goog-expires", "x-goog-signedheaders"})
PINNED_PAGINATION = frozenset({"page-token", "next-token", "continuation-token", "cursor", "pagetoken",
                               "nextpagetoken"})


def test_the_lists_are_the_pinned_ones() -> None:
    assert strip.CRED_STRIP_HEADERS == PINNED_HEADERS
    assert {strip.fold(n) for n in strip.CRED_STRIP_QUERY} == {strip.fold(n) for n in PINNED_QUERY}
    assert strip.SIGNED_URL_READ_EXEMPT == PINNED_SIGNED
    from levain.chat_egress import classify
    assert classify._SIGNED_URL_READ_EXEMPT == strip.SIGNED_URL_READ_EXEMPT   # the decider stores by one, the report counts by the other
    assert strip.PAGINATION_NOT_CREDENTIALS == PINNED_PAGINATION


@pytest.mark.parametrize("name,matches", [
    ("auth", True), ("authorization", True), ("x-auth-key", True), ("authx", True), ("x-passwd", True),
    ("sessionid", True), ("x-vault-token", True), ("x-deploy-key", False), ("oauthish", False),
    ("secret", True), ("credential", True), ("signature", True), ("api-key", True), ("cursor", False),
    ("author", True), ("sig", False),     # `sig` joins the QUERY backstop only, exact
])
def test_the_header_backstop_spelling_is_pinned(name, matches) -> None:
    assert strip.header_is_credential(name) is matches


@pytest.mark.parametrize("name,matches", [("sig", True), ("signature", True), ("sigx", False), ("design", False),
                                          ("token", True), ("page-token", False), ("author", True)])
def test_the_query_backstop_spelling_is_pinned(name, matches) -> None:
    assert strip.query_name_is_credential(name) is matches


# --- the clients ------------------------------------------------------------------------------------------------

HEADERS = [("Authorization", "Bearer CORPUSAUTH1"), ("Proxy-Authorization", "Basic CORPUSPROXY1"),
           ("Cookie", "s=CORPUSCOOKIE1"), ("X-Api-Key", "CORPUSAPIKEY1"), ("Api-Key", "CORPUSAPIKEY2"),
           ("X-Goog-Api-Key", "CORPUSGOOG1"), ("Ocp-Apim-Subscription-Key", "CORPUSOCP1"),
           ("Private-Token", "CORPUSPRIV1"), ("Job-Token", "CORPUSJOB1"), ("X-Auth-Token", "CORPUSXAUTH1"),
           ("X-Access-Token", "CORPUSXACC1"), ("X-Amz-Security-Token", "CORPUSAMZ1"),
           ("X-Vault-Token", "CORPUSVAULT1"), ("X-Session-Id", "CORPUSSESS1")]     # the last two: the backstop's
VALUES = [v.rsplit(" ", 1)[-1].split("=")[-1] for _, v in HEADERS]
LISTED = {strip.fold(k) for k, _ in HEADERS}


def _env(tmp_path, port: int) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    proxy = f"http://127.0.0.1:{port}"
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "HTTP_PROXY": proxy, "http_proxy": proxy,
            "NO_PROXY": "", "no_proxy": "", "GIT_TERMINAL_PROMPT": "0", "XDG_CONFIG_HOME": str(home / ".config")}


def _url(L, path="/c") -> str:
    return f"http://pub.rb.test:{L.origin_port}{path}"


def run_curl(L, tmp_path):
    cmd = ["curl", "-sS", "--max-time", "20", "-x", f"http://127.0.0.1:{L.port}", _url(L)]
    for k, v in HEADERS:
        cmd += ["-H", f"{k}: {v}"]
    return subprocess.run(cmd, env=_env(tmp_path, L.port), capture_output=True, text=True, timeout=40)


def run_requests(L, tmp_path):
    pytest.importorskip("requests")
    code = ("import requests,sys; h=dict(); \n"
            "import json; hs=json.loads(sys.argv[2]); s=requests.Session(); s.trust_env=False\n"
            "r=s.get(sys.argv[1], headers=hs, proxies={'http': sys.argv[3]}, timeout=20); print(r.status_code)")
    return subprocess.run([sys.executable, "-I", "-c", code, _url(L), json.dumps(dict(HEADERS)),
                           f"http://127.0.0.1:{L.port}"], env=_env(tmp_path, L.port), capture_output=True, text=True,
                          timeout=40)


def run_git(L, tmp_path):
    cmd = ["git", "-c", f"http.proxy=http://127.0.0.1:{L.port}"]
    for k, v in HEADERS:
        cmd += ["-c", f"http.extraHeader={k}: {v}"]
    cmd += ["ls-remote", _url(L, "/r.git")]
    return subprocess.run(cmd, env=_env(tmp_path, L.port), capture_output=True, text=True, timeout=40)


def run_pip(L, tmp_path):
    pip = shutil.which("pip3") or shutil.which("pip")
    return subprocess.run([pip, "download", "corpuspkg", "--no-deps", "--no-cache-dir", "--retries", "0", "--timeout", "10",
                           "--index-url", f"http://corpus:PIPPASS1@pub.rb.test:{L.origin_port}/simple/",
                           "--proxy", f"http://127.0.0.1:{L.port}", "-d", str(tmp_path / "dl"),
                           "--trusted-host", "pub.rb.test"],
                          env=_env(tmp_path, L.port), capture_output=True, text=True, timeout=60)


def run_npm(L, tmp_path):
    rc = tmp_path / "npmrc"
    rc.write_text(f"//pub.rb.test:{L.origin_port}/:_authToken=NPMTOKEN1\n")
    return subprocess.run(["npm", "view", "corpuspkg", "--registry", f"http://pub.rb.test:{L.origin_port}/",
                           "--proxy", f"http://127.0.0.1:{L.port}", "--userconfig", str(rc), "--fetch-retries=0",
                           "--fetch-timeout=10000", "--cache", str(tmp_path / "npmcache")],
                          env=_env(tmp_path, L.port), capture_output=True, text=True, timeout=60)


def run_uv(L, tmp_path):
    env = _env(tmp_path, L.port)
    return subprocess.run(["uv", "pip", "install", "corpuspkg", "--no-cache", "--python", sys.executable,
                           "--index-url", f"http://corpus:UVPASS1@pub.rb.test:{L.origin_port}/simple/",
                           "--dry-run"], env=env, capture_output=True, text=True, timeout=60)


# (name, runner, binary on PATH, the values the origin must never see, the folded header names the rows must name)
CLIENTS = [
    ("curl", run_curl, "curl", VALUES, LISTED),
    ("requests", run_requests, None, VALUES, LISTED),
    ("git", run_git, "git", VALUES, LISTED),
    ("pip", run_pip, "pip3", ["PIPPASS1"], {"authorization"}),
    ("npm", run_npm, "npm", ["NPMTOKEN1"], {"authorization"}),
    ("uv", run_uv, "uv", ["UVPASS1"], {"authorization"}),
]


@pytest.mark.parametrize("name,runner,binary,values,named", CLIENTS, ids=[c[0] for c in CLIENTS])
def test_a_real_client_carrying_credentials_reaches_the_origin_with_none(
        make_origin, make_launch_, tmp_path, name, runner, binary, values, named) -> None:
    if binary is not None and shutil.which(binary) is None and not (name == "pip" and shutil.which("pip")):
        pytest.skip(f"{name} is not installed on this machine (command -v {binary})")
    origin = make_origin()
    L = make_launch_(origin)
    proc = runner(L, tmp_path)
    assert L.stop() == 0
    # a client that sent nothing proves nothing: the origin saw its request, and the rows name the stripped credentials
    assert origin.reqs, f"{name} reached the origin with no request: rc={proc.returncode} stderr={proc.stderr[-400:]!r}"
    for seen in origin.reqs:
        for k, v in seen.headers:
            assert strip.fold(k) not in strip.CRED_STRIP_HEADERS, (name, k)
        for val in values:
            assert val.encode() not in seen.raw_head and val.encode() not in seen.body, (name, val)
    rows = [r for r in L.records.rows(L.launch_id) if r.kind == "egress_attempt"]
    recorded = set()
    for r in rows:
        recorded |= {n for n in json.loads(r.names or "[]")}
    assert named <= recorded, (name, sorted(named - recorded), sorted(recorded))
    # none of the values in any file the run left
    from .rig import sweep
    assert sweep(L.dir, {f"v{i}": v for i, v in enumerate(values)}) == []


# --- the query names, by curl ------------------------------------------------------------------------------------

def _curl_get(L, tmp_path, query: str):
    return subprocess.run(["curl", "-sS", "--max-time", "20", "-o", "/dev/null", "-w", "%{http_code}",
                           "-x", f"http://127.0.0.1:{L.port}", _url(L, "/q") + "?" + query],
                          env=_env(tmp_path, L.port), capture_output=True, text=True, timeout=40)


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed on this machine")
def test_curl_with_each_listed_query_name_is_carded_never_forwarded_gated(make_origin, make_launch_, tmp_path) -> None:
    from .rig import autoanswer
    origin = make_origin()
    L = make_launch_(origin)
    stop = autoanswer(L, "denied")
    try:
        for n in sorted(PINNED_QUERY):
            p = _curl_get(L, tmp_path, f"{n}=QV{n.upper()}")
            assert p.stdout.strip() == "403", (n, p.stdout, p.stderr[-200:])
        assert L.stop() == 0
    finally:
        stop.set()
    assert origin.reqs == []
    steps = {r.deciding_step for r in L.records.rows(L.launch_id) if r.kind == "egress_attempt"}
    assert steps == {"card:credential_in_query:denied"}, steps


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed on this machine")
def test_curl_with_each_listed_query_name_is_a_wall_under_open_network(make_origin, make_launch_, tmp_path) -> None:
    origin = make_origin()
    L = make_launch_(origin, mode=classify.OPEN_NETWORK)
    for n in sorted(PINNED_QUERY):
        p = _curl_get(L, tmp_path, f"{n}=QV{n.upper()}")
        assert p.stdout.strip() == "403", (n, p.stdout)
    assert L.stop() == 0
    assert origin.reqs == []
    steps = {r.deciding_step for r in L.records.rows(L.launch_id) if r.kind == "egress_attempt"}
    assert steps == {"wall:credential_in_query"}, steps


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed on this machine")
def test_curl_signed_url_read_is_silent_and_pagination_names_are_not_credentials(make_origin, make_launch_,
                                                                                tmp_path) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    q = "&".join(f"{n}=SV{i}" for i, n in enumerate(sorted(PINNED_SIGNED - {"x-amz-security-token"})))
    assert _curl_get(L, tmp_path, q).stdout.strip() == "200"
    for n in sorted(PINNED_PAGINATION):
        assert _curl_get(L, tmp_path, f"{n}=PV").stdout.strip() == "200", n
    assert L.stop() == 0
    assert len(origin.reqs) == 1 + len(PINNED_PAGINATION)
    steps = {r.deciding_step for r in L.records.rows(L.launch_id) if r.kind == "egress_attempt"}
    assert steps == {"silent"}, steps


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed on this machine")
def test_k1_can_count_a_signed_url_exemption_from_the_rows(make_origin, make_launch_, tmp_path) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    assert _curl_get(L, tmp_path, "X-Amz-Signature=SV1&X-Amz-Credential=SV2").stdout.strip() == "200"
    assert L.stop() == 0
    from levain.chat_egress import report
    r = report.build_from_files(L.records.rows(L.launch_id), L.events)
    assert r.signed_url_names == {"x-amz-signature": 1, "x-amz-credential": 1}, r.signed_url_names
