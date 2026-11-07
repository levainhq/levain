"""PATHZERO Z2b: the seat's GitHub read door (levain.seat_gh and the levain_gh MCP server).

No real token is used anywhere: ``gh_read`` talks to a fake HTTP server on 127.0.0.1, ``git_fetch`` fetches from a local
bare repository through the ``file`` transport. Each test names the line of ``levain/seat_gh.py`` whose revert turns it
red (the mutation record is in RETURN_z2b_build.md)."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from levain import seat_gh as g
from levain import seat_gh_mcp as mcp

TOKEN_L = "github_pat_" + "L" * 40          # fake
TOKEN_P = "github_pat_" + "P" * 40          # fake
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
FUTURE = "2026-12-31"
PAST = "2026-11-01"


class Fake:
    """A local stand-in for api.github.com: records every request; ``reply`` decides the answer."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = lambda req: (200, {}, b'{"ok": true}')
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                req = {"method": "GET", "path": self.path, "headers": dict(self.headers)}
                outer.requests.append(req)
                status, headers, body = outer.reply(req)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "lhome"
    h.mkdir()
    return h


def install(home, owner="levainhq", token=TOKEN_L, expires=FUTURE):
    g._write_record(home, owner, token, g._parse_expiry(expires))


def both(home):
    install(home, "levainhq", TOKEN_L)
    install(home, "phillipclapham", TOKEN_P)


def log_text(home) -> str:
    p = home / "push" / g.LOG_NAME
    return p.read_text() if p.exists() else ""


# --- the allowlists ---------------------------------------------------------------------------------------------------

ALLOWED = ["/repos/levainhq/levain", "/repos/phillipclapham/flow", "/repos/levainhq/levain/contents/levain/cli.py",
           "/repos/phillipclapham/flow/pulls/12", "/repos/levainhq/levain/actions/runs", "/repos/levainhq/levain/commits",
           "/repos/levainhq/levain/branches/main", "/repos/levainhq/levain/issues/3/comments",
           "/repos/phillipclapham/flow/contents/.github/workflows/ci.yml"]


@pytest.mark.parametrize("path", ALLOWED)
def test_the_allowed_paths_pass(path):
    assert g.path_refusal(path) is None


@pytest.mark.parametrize("path", [
    "/repos/evil/repo", "/repos/levainhq/other", "/repos/LevainHQ/levain", "/repos/levainhq/levain-evil",
    "/repos/phillipclapham/flow2", "/user", "/orgs/levainhq", "/repos/levainhq/levain/hooks",
    "/repos/levainhq/levain/git/blobs/abc", "/repos/levainhq/levain/keys", "/repos/levainhq/levain/collaborators",
    "/repos/levainhq/levain/actions/secrets", "/repos/levainhq/levain/actions/variables",
    "/repos/levainhq/levain/actions/runners", "/repos/levainhq/levain/contents/../../../user",
    "/repos/levainhq/levain/contents/%2e%2e/x", "/repos/levainhq/levain/contents/a%2Fb", "/repos/levainhq/levain/",
    "/repos/levainhq/levain//contents", "/repos/levainhq/levain/contents/a b", "/repos/levainhq/levain/contents/x?y=1",
    "/repos/levainhq/levain/contents/x#f", "/repos/levainhq/levain/contents\\x", "/repos/levainhq/levain/contents/./x",
    "repos/levainhq/levain", "https://api.github.com/repos/levainhq/levain", "//evil.example/repos/levainhq/levain",
    "/repos/levainhq/levain/../../evil/x", "", None, 7, "/repos/levainhq/levain\n/contents"])
def test_a_path_outside_the_allowlist_is_refused(path):
    """Revert: ``if repo not in REPOS`` / ``segs[0] not in READ_SEGMENTS`` / ``segs[1] not in ACTIONS_CHILDREN`` / the ``..`` check / the ``_PATH`` alphabet."""
    assert g.path_refusal(path) in (g.R_PATH, g.R_REPO)


def test_the_repo_allowlist_is_exactly_the_two_with_their_owners():
    assert dict(g.REPOS) == {"levainhq/levain": "levainhq", "phillipclapham/flow": "phillipclapham"}
    assert g.READ_SEGMENTS == {"contents", "pulls", "issues", "actions", "commits", "branches"}


@pytest.mark.parametrize("q", [{"token": "x"}, {"ref": "a;b"}, {"ref": "x" * 201}, {"ref": ["a"]}, "ref=main",
                               {f"page": 1, "per_page": 2, "ref": "m", "state": "a", "sha": "b", "path": "c",
                                "branch": "d", "event": "e", "status": "f", "since": "g", "until": "h"}])
def test_a_query_outside_the_allowlist_is_refused(q):
    """Revert: the ``QUERY_KEYS`` / ``_QUERY_VALUE`` / ``_QUERY_MAX`` checks of ``query_refusal``."""
    assert g.query_refusal(q) == g.R_QUERY


def test_a_plain_query_passes():
    assert g.query_refusal({"ref": "seat/z2b", "per_page": "30"}) is None and g.query_refusal(None) is None


# --- gh_read ----------------------------------------------------------------------------------------------------------

def test_gh_read_sends_one_get_with_the_owners_token(home, fake):
    both(home)
    status, body = g.gh_read(home, "/repos/phillipclapham/flow/contents/README.md", {"ref": "main"},
                             api_base=fake.base, now=NOW)
    assert status == 200 and json.loads(body) == {"ok": True}
    (req,) = fake.requests
    assert req["method"] == "GET" and req["path"] == "/repos/phillipclapham/flow/contents/README.md?ref=main"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN_P}"          # the path's OWNER's token
    fake.requests.clear()
    g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN_L}"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "get", "HEAD", ""])
def test_gh_read_refuses_a_non_get(home, fake, method):
    """Revert: ``if method != "GET"`` in ``gh_read``."""
    both(home)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", method=method, api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_METHOD and fake.requests == []


def test_gh_read_refuses_another_repo_before_any_request(home, fake):
    """Revert: the ``path_refusal`` call in ``gh_read``."""
    both(home)
    for path in ("/repos/evil/x", "/repos/levainhq/levain/contents/../../x", "/user"):
        with pytest.raises(g.GhRefused):
            g.gh_read(home, path, api_base=fake.base, now=NOW)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", {"access_token": "x"}, api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_QUERY
    assert fake.requests == []


@pytest.mark.parametrize("base", ["https://evil.example", "https://api.github.com.evil.example", "http://api.github.com",
                                  "http://evil.example", "https://127.0.0.1", "ftp://127.0.0.1", "http://10.0.0.1:80",
                                  "https://api.github.com/extra", "https://api.github.com:8443"])
def test_gh_read_never_sends_the_token_to_another_host(home, base):
    """Revert: the host / scheme / loopback conditions of ``_connection``."""
    both(home)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=base, now=NOW)
    assert str(e.value) == g.R_HOST


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_gh_read_follows_no_redirect(home, fake, code):
    """Revert: the ``300 <= status < 400`` branch of ``_get``."""
    both(home)
    fake.reply = lambda req: (code, {"Location": "http://127.0.0.1:1/steal"}, b"")
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_REDIRECT and len(fake.requests) == 1


def test_gh_read_refuses_with_no_token_and_no_fallback(home, fake, monkeypatch):
    """Revert: ``raise GhRefused(R_TOKEN_ABSENT)`` in ``_read_record`` (a fallback would send some other credential)."""
    for k, v in (("GH_TOKEN", "ghp_" + "e" * 30), ("GITHUB_TOKEN", "ghp_" + "f" * 30), ("HOME", str(home))):
        monkeypatch.setenv(k, v)
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: ghp_" + "h" * 30 + "\n")
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_ABSENT and fake.requests == []
    install(home, "levainhq")                               # one owner's token does not serve the other owner
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/phillipclapham/flow", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_ABSENT and fake.requests == []


def test_gh_read_refuses_an_expired_token(home, fake):
    """Revert: ``if now >= expires`` in ``_live_token``."""
    install(home, "levainhq", expires=PAST)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_EXPIRED and fake.requests == []
    install(home, "levainhq", expires=(NOW + timedelta(seconds=1)).isoformat())
    assert g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)[0] == 200
    install(home, "levainhq", expires=NOW.isoformat())      # expires AT now: expired
    with pytest.raises(g.GhRefused):
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)


def test_gh_read_refuses_a_token_store_that_is_not_private(home, fake):
    """Revert: the ``0o077`` mode checks and the symlink/ownership checks in ``_read_record``."""
    both(home)
    f = home / "seat_gh" / "levainhq"
    os.chmod(f, 0o644)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_BAD
    os.chmod(f, 0o600)
    os.chmod(home / "seat_gh", 0o755)
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_BAD
    os.chmod(home / "seat_gh", 0o700)
    real = home / "elsewhere"
    real.write_text(f.read_text())
    os.chmod(real, 0o600)
    f.unlink()
    f.symlink_to(real)                                      # a link in the store is not followed
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_BAD
    assert fake.requests == []


def test_gh_read_scrubs_the_token_from_what_it_returns_and_logs_none(home, fake):
    """Revert: the ``scrub(...)`` call on the body in ``gh_read`` (and the log row never holds it)."""
    both(home)
    fake.reply = lambda req: (200, {}, f'{{"echo": "{TOKEN_L}", "q": "{g.quote(TOKEN_L)}", "other": "ghp_{"z" * 30}"}}'.encode())
    status, body = g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert TOKEN_L not in body and "ghp_" not in body and body.count("[token]") == 3
    assert TOKEN_L not in log_text(home) and TOKEN_P not in log_text(home) and log_text(home)


def test_a_non_2xx_answer_is_returned_scrubbed_not_raised(home, fake):
    both(home)
    fake.reply = lambda req: (404, {}, f'{{"message": "Not Found {TOKEN_L}"}}'.encode())
    status, body = g.gh_read(home, "/repos/levainhq/levain/contents/x", api_base=fake.base, now=NOW)
    assert status == 404 and TOKEN_L not in body


def test_the_body_is_capped(home, fake):
    both(home)
    fake.reply = lambda req: (200, {}, b"x" * (g.MAX_BODY * 2))
    assert len(g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)[1]) <= g.MAX_BODY


def test_the_token_is_never_in_an_argument_or_the_environment(home, fake, monkeypatch):
    """gh_read starts no process; git_fetch's argv and env are asserted in the fetch tests below."""
    both(home)
    spawned = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append((a, k)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))
    g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert spawned == []


# --- the token store and the commands --------------------------------------------------------------------------------

def test_the_token_files_are_0600_in_a_0700_dir(home):
    both(home)
    assert stat.S_IMODE(os.lstat(home / "seat_gh").st_mode) == 0o700
    for owner in g.OWNERS:
        assert stat.S_IMODE(os.lstat(home / "seat_gh" / owner).st_mode) == 0o600


def test_set_token_checks_the_token_with_a_host_get_and_records_a_fingerprint(home, fake):
    fake.reply = lambda req: (200, {"github-authentication-token-expiration": "2026-12-01 00:00:00 UTC"}, b"{}")
    line = g.set_token(home, "levainhq", TOKEN_L + "\n", api_base=fake.base, now=NOW)
    assert TOKEN_L not in line and g.token_fingerprint(TOKEN_L) in line
    (req,) = fake.requests
    assert req["path"] == "/repos/levainhq/levain" and req["headers"]["Authorization"] == f"Bearer {TOKEN_L}"
    rec = json.loads((home / "seat_gh" / "levainhq").read_text())
    assert rec["fingerprint"] == g.token_fingerprint(TOKEN_L) == __import__("hashlib").sha256(TOKEN_L.encode()).hexdigest()[:12]
    assert rec["expires_at"].startswith("2026-12-01T00:00:00")          # GitHub's header wins
    assert TOKEN_L not in line and g.token_status(home, "levainhq", NOW).count(TOKEN_L) == 0


def test_set_token_for_the_other_owner_checks_that_owners_repo(home, fake):
    g.set_token(home, "phillipclapham", TOKEN_P, expires=FUTURE, api_base=fake.base, now=NOW)
    assert fake.requests[0]["path"] == "/repos/phillipclapham/flow"


def test_set_token_stores_nothing_when_the_check_fails_or_the_expiry_is_unknown_or_past(home, fake):
    fake.reply = lambda req: (401, {}, b"{}")
    with pytest.raises(g.GhRefused, match="nothing was stored"):
        g.set_token(home, "levainhq", TOKEN_L, expires=FUTURE, api_base=fake.base, now=NOW)
    fake.reply = lambda req: (200, {}, b"{}")
    with pytest.raises(g.GhRefused, match="expiry is unknown"):
        g.set_token(home, "levainhq", TOKEN_L, api_base=fake.base, now=NOW)
    with pytest.raises(g.GhRefused, match="already expired"):
        g.set_token(home, "levainhq", TOKEN_L, expires=PAST, api_base=fake.base, now=NOW)
    with pytest.raises(g.GhRefused, match="not a date"):
        g.set_token(home, "levainhq", TOKEN_L, expires="soon", api_base=fake.base, now=NOW)
    for bad in ("short", "has space " + "x" * 30, "ghp_" + "x" * 30 + "\n--flag", ""):
        with pytest.raises(g.GhRefused, match="not a GitHub token"):
            g.set_token(home, "levainhq", bad, expires=FUTURE, api_base=fake.base, now=NOW)
    with pytest.raises(g.GhRefused):
        g.set_token(home, "someone-else", TOKEN_L, expires=FUTURE, api_base=fake.base, now=NOW)
    assert not (home / "seat_gh" / "levainhq").exists()


def test_clear_token_deletes_it_and_the_next_verb_refuses(home, fake):
    both(home)
    assert g.clear_token(home, "levainhq") is True and g.clear_token(home, "levainhq") is False
    with pytest.raises(g.GhRefused) as e:
        g.gh_read(home, "/repos/levainhq/levain", api_base=fake.base, now=NOW)
    assert str(e.value) == g.R_TOKEN_ABSENT
    assert g.gh_read(home, "/repos/phillipclapham/flow", api_base=fake.base, now=NOW)[0] == 200


def test_status_prints_no_token(home):
    both(home)
    for owner in g.OWNERS:
        line = g.token_status(home, owner, NOW)
        assert "live" in line and "fingerprint" in line and TOKEN_L not in line and TOKEN_P not in line
    assert "EXPIRED" in g.token_status(home, "levainhq", NOW + timedelta(days=400))


def test_the_token_is_read_from_stdin_only():
    class Pipe(io.StringIO):
        def isatty(self):
            return False
    assert g.read_token_stdin(Pipe(TOKEN_L + "\n")).strip() == TOKEN_L
    import inspect
    assert "argv" not in inspect.getsource(g.read_token_stdin) and "sys.argv" not in inspect.getsource(g.set_token)


def test_the_cli_has_no_token_argument():
    from levain import cli
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is None:
        pytest.skip("no build_parser")
    with pytest.raises(SystemExit):
        p.parse_args(["seat", "gh-token", "set", "--owner", "levainhq", "--token", TOKEN_L])


def test_the_cli_installs_clears_and_reports_from_stdin(home, fake, monkeypatch, capsys):
    from levain import cli, seat_lease
    monkeypatch.setattr(seat_lease, "_levain_home", lambda: home)
    monkeypatch.setattr(g, "API_BASE", fake.base)
    monkeypatch.setattr(g.set_token, "__kwdefaults__", {**(g.set_token.__kwdefaults__ or {}), "api_base": fake.base})
    class Pipe(io.StringIO):
        def isatty(self):
            return False
    monkeypatch.setattr(sys, "stdin", Pipe(TOKEN_L + "\n"))
    rc = cli.main(["seat", "gh-token", "set", "--owner", "levainhq", "--expires", FUTURE])
    out = capsys.readouterr()
    assert rc == 0 and "installed: levainhq" in out.out and TOKEN_L not in out.out + out.err
    assert cli.main(["seat", "gh-token", "status"]) == 0
    assert TOKEN_L not in capsys.readouterr().out
    assert cli.main(["seat", "gh-token", "clear", "--owner", "levainhq"]) == 0
    assert not (home / "seat_gh" / "levainhq").exists()


# --- git_fetch --------------------------------------------------------------------------------------------------------

def git(*a, cwd=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["/usr/bin/git", *a], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A local 'GitHub': <root>/levainhq/levain.git with one commit on main and one on a feature branch."""
    root = tmp_path / "gh"
    bare = root / "levainhq" / "levain.git"
    work = tmp_path / "work"
    bare.parent.mkdir(parents=True)
    git("init", "--bare", "--quiet", "-b", "main", str(bare))
    git("init", "--quiet", "-b", "main", str(work))
    (work / "f.txt").write_text("hello\n")
    git("add", "f.txt", cwd=work)
    git("commit", "-qm", "one", cwd=work)
    git("checkout", "-qb", "feat", cwd=work)
    (work / "f.txt").write_text("hello feat\n")
    git("commit", "-qam", "two", cwd=work)
    git("push", "-q", str(bare), "main", "feat", cwd=work)
    return str(root) + "/"


def fetch(home, upstream, repo="levainhq/levain"):
    return g.git_fetch(home, repo, git_base="file://" + upstream, now=NOW)


def test_git_fetch_fills_a_levain_owned_mirror_the_seat_can_fetch_from(home, upstream, tmp_path):
    both(home)
    out = fetch(home, upstream)
    mirror = home / "gh_mirror" / "levainhq" / "levain.git"
    assert f"mirror: {mirror}" in out and "refs/heads" not in out and "main" in out and "feat" in out
    assert git("--git-dir", str(mirror), "rev-parse", "feat") == git("--git-dir", f"{upstream}levainhq/levain.git", "rev-parse", "feat")
    assert stat.S_IMODE(os.lstat(mirror).st_mode) == 0o700 and not (mirror / "hooks").exists()
    # the seat's side: a clone fetches from the LOCAL path, no network
    clone = tmp_path / "clone"
    git("init", "--quiet", str(clone))
    git("fetch", "-q", str(mirror), "feat", cwd=clone)
    assert git("rev-parse", "FETCH_HEAD", cwd=clone) == git("--git-dir", str(mirror), "rev-parse", "feat")
    # the mirror lives OUTSIDE the denied token directory
    assert not str(mirror).startswith(str(home / g.TOKEN_DIR) + os.sep)


def test_git_fetch_is_incremental_and_prunes(home, upstream, tmp_path):
    both(home)
    fetch(home, upstream)
    bare = f"{upstream}levainhq/levain.git"
    git("--git-dir", bare, "branch", "-D", "feat")
    out = fetch(home, upstream)
    assert "feat" not in out and "main" in out


def test_git_fetch_refuses_another_repo(home, upstream):
    """Revert: ``repo not in REPOS`` in ``git_fetch``."""
    both(home)
    for repo in ("evil/repo", "levainhq/other", "../x", None, "levainhq/levain.git", "levainhq/levain; rm -rf /"):
        with pytest.raises(g.GhRefused) as e:
            fetch(home, upstream, repo)
        assert str(e.value) == g.R_REPO
    assert not (home / "gh_mirror").exists()


def test_git_fetch_refuses_before_any_git_with_no_or_expired_token(home, upstream, monkeypatch):
    """Revert: the ``_live_token`` call (placed before ``push.git_bin()``) in ``git_fetch``."""
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(g.GhRefused) as e:
        fetch(home, upstream)
    assert str(e.value) == g.R_TOKEN_ABSENT
    install(home, "levainhq", expires=PAST)
    with pytest.raises(g.GhRefused) as e:
        fetch(home, upstream)
    assert str(e.value) == g.R_TOKEN_EXPIRED and ran == [] and not (home / "gh_mirror").exists()


@pytest.mark.parametrize("base", ["http://github.com/", "ssh://git@github.com/", "git://github.com/", "https://github.com",
                                  "ftp://x/"])
def test_git_fetch_allows_only_https_or_file_remote_bases(home, base):
    both(home)
    with pytest.raises(g.GhRefused) as e:
        g.git_fetch(home, "levainhq/levain", git_base=base, now=NOW)
    assert str(e.value) == g.R_HOST


def test_the_git_child_gets_a_rebuilt_environment_and_the_token_is_in_no_argument(home, upstream, monkeypatch):
    """Revert: ``_git_env`` returning the caller's env, or the helper command / extra config carrying the token."""
    both(home)
    monkeypatch.setenv("GIT_DIR", "/tmp/evil")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "9" * 30)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    seen = []
    real_run = subprocess.run

    def spy(argv, **kw):
        seen.append((list(argv), dict(kw.get("env") or {})))
        return real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    fetch(home, upstream)
    fetch_calls = [(a, e) for a, e in seen if "fetch" in a]
    assert fetch_calls
    for argv, env in seen:
        assert TOKEN_L not in " ".join(argv) and not any(TOKEN_L in v for v in env.values())
        assert set(env) == {"PATH", "HOME", "LANG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
                            "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_ALLOW_PROTOCOL"}
        assert env["GIT_ALLOW_PROTOCOL"] == "file" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["HOME"] != os.environ["HOME"] and "GH_TOKEN" not in env and "SSH_AUTH_SOCK" not in env
    argv = fetch_calls[0][0]
    assert "credential.helper=" in argv and "credential.useHttpPath=true" in argv and "http.followRedirects=false" in argv
    assert any(a.startswith("credential.https://github.com.helper=!") and "levain.seat_gh credential-helper" in a for a in argv)
    assert argv[-2:] == [f"file://{upstream}levainhq/levain.git", "+refs/heads/*:refs/heads/*"] or argv[-1] == "+refs/heads/*:refs/heads/*"
    assert TOKEN_L not in log_text(home)


def test_a_failed_fetch_names_no_token(home, tmp_path):
    both(home)
    with pytest.raises(g.GhRefused) as e:
        g.git_fetch(home, "levainhq/levain", git_base="file://" + str(tmp_path / "nowhere") + "/", now=NOW)
    assert str(e.value).startswith(g.R_FETCH) and TOKEN_L not in str(e.value)


def test_git_fetch_scrubs_the_token_from_git_output(home, upstream, monkeypatch):
    """Revert: the ``scrub(...)`` calls in ``git_fetch``."""
    both(home)
    real_run = subprocess.run

    class R:
        returncode = 128
        stdout = ""
        stderr = f"fatal: unable to access 'https://x-access-token:{TOKEN_L}@github.com/levainhq/levain.git/'"

    def fake_run(argv, **kw):
        return R() if "fetch" in argv else real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(g.GhRefused) as e:
        fetch(home, upstream)
    assert TOKEN_L not in str(e.value) and "[token]" in str(e.value) and TOKEN_L not in log_text(home)


# --- the credential helper --------------------------------------------------------------------------------------------

def helper(home, **req):
    out = io.StringIO()
    g.credential_helper("get", "".join(f"{k}={v}\n" for k, v in req.items()), home, out, now=NOW)
    return out.getvalue()


def test_the_helper_answers_only_github_https_and_the_allowed_repo_path(home):
    """Revert: any of the ``protocol`` / ``host`` / ``repo not in REPOS`` conditions in ``credential_helper``."""
    both(home)
    ok = helper(home, protocol="https", host="github.com", path="levainhq/levain.git")
    assert ok == f"username=x-access-token\npassword={TOKEN_L}\n"
    assert helper(home, protocol="https", host="github.com", path="phillipclapham/flow.git").endswith(f"password={TOKEN_P}\n")
    for req in ({"protocol": "https", "host": "evil.example", "path": "levainhq/levain.git"},
                {"protocol": "https", "host": "github.com.evil.example", "path": "levainhq/levain.git"},
                {"protocol": "http", "host": "github.com", "path": "levainhq/levain.git"},
                {"protocol": "ssh", "host": "github.com", "path": "levainhq/levain.git"},
                {"protocol": "https", "host": "github.com", "path": "evil/repo.git"},
                {"protocol": "https", "host": "github.com", "path": "levainhq/levain"},
                {"protocol": "https", "host": "github.com", "path": "levainhq/levain.git/../../x.git"},
                {"protocol": "https", "host": "github.com"}):
        assert helper(home, **req) == ""


def test_the_helper_answers_nothing_for_an_expired_or_absent_token_and_for_store_and_erase(home):
    install(home, "levainhq", expires=PAST)
    assert helper(home, protocol="https", host="github.com", path="levainhq/levain.git") == ""
    assert helper(home, protocol="https", host="github.com", path="phillipclapham/flow.git") == ""
    both(home)
    out = io.StringIO()
    for action in ("store", "erase"):
        g.credential_helper(action, "protocol=https\nhost=github.com\npath=levainhq/levain.git\n", home, out, now=NOW)
    assert out.getvalue() == ""


def test_the_helper_as_a_real_subprocess_speaks_gits_protocol(home):
    both(home)
    root = str(Path(g.__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-B", "-m", "levain.seat_gh", "credential-helper", "--home", str(home), "get"],
                       input="protocol=https\nhost=github.com\npath=levainhq/levain.git\n\n", capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": root}, cwd=root)
    assert r.returncode == 0
    # (the profile's hook flags -EP are the launcher's; here PYTHONPATH finds the worktree's levain)
    assert r.stdout == f"username=x-access-token\npassword={TOKEN_L}\n"


# --- scrub ------------------------------------------------------------------------------------------------------------

def test_scrub_covers_the_raw_quoted_and_base64_forms():
    import base64
    t = TOKEN_L
    text = f"{t} {g.quote(t)} {base64.b64encode(f'x-access-token:{t}'.encode()).decode()} ghp_{'a' * 30} ghs_{'b' * 30}"
    out = g.scrub(text, (t,))
    assert t not in out and "ghp_" not in out and "ghs_" not in out and "x-access" not in out
    assert g.scrub("nothing here", (t, "")) == "nothing here"


# --- the MCP server ---------------------------------------------------------------------------------------------------

def rpc(home, method, params=None, rid=1):
    return mcp.handle(str(home), {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})


def test_the_server_names_itself_levain_gh_and_lists_exactly_two_tools(home):
    init = rpc(home, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "levain_gh"
    tools = rpc(home, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["gh_read", "git_fetch"]
    for t in tools:                                         # no method, host, url, token or header field exists
        assert t["inputSchema"]["additionalProperties"] is False
        assert not {"method", "host", "url", "token", "headers", "base"} & set(t["inputSchema"]["properties"])


def test_the_server_refuses_with_fixed_words_and_extra_fields(home, monkeypatch, fake):
    both(home)
    monkeypatch.setattr(g, "API_BASE", fake.base)
    r = rpc(home, "tools/call", {"name": "gh_read", "arguments": {"path": "/repos/evil/x"}})["result"]
    assert r["isError"] and r["content"][0]["text"] == g.R_REPO
    r = rpc(home, "tools/call", {"name": "gh_read", "arguments": {"path": "/repos/levainhq/levain", "method": "POST"}})["result"]
    assert r["isError"] and r["content"][0]["text"] == g.R_PATH
    r = rpc(home, "tools/call", {"name": "gh_read", "arguments": {"path": "/repos/levainhq/levain", "token": "x"}})["result"]
    assert r["isError"]
    r = rpc(home, "tools/call", {"name": "git_fetch", "arguments": {"repo": "evil/x"}})["result"]
    assert r["isError"] and r["content"][0]["text"] == g.R_REPO
    assert rpc(home, "tools/call", {"name": "delete_repo", "arguments": {}})["error"]["code"] == mcp.INVALID_PARAMS
    assert fake.requests == []


def test_the_server_absent_token_is_one_fixed_sentence(home):
    r = rpc(home, "tools/call", {"name": "gh_read", "arguments": {"path": "/repos/levainhq/levain"}})["result"]
    assert r["isError"] and r["content"][0]["text"] == g.R_TOKEN_ABSENT


def test_the_server_returns_a_scrubbed_read_over_stdio(home, fake):
    """The real server in a subprocess against the fake API: the answer never carries the token."""
    both(home)
    fake.reply = lambda req: (200, {}, f'{{"leak": "{TOKEN_L}"}}'.encode())
    root = str(Path(g.__file__).resolve().parent.parent)
    code = ("import sys; sys.path.insert(0, %r); from levain import seat_gh as g, seat_gh_mcp as m; "
            "g.API_BASE = %r; g.gh_read.__kwdefaults__['api_base'] = %r; m.main(['--home', %r])" % (root, fake.base, fake.base, str(home)))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "gh_read", "arguments": {"path": "/repos/levainhq/levain"}}}]
    r = subprocess.run([sys.executable, "-c", code], input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n",
                       capture_output=True, text=True, timeout=60)
    lines = [json.loads(x) for x in r.stdout.splitlines()]
    assert len(lines) == 3 and lines[0]["result"]["serverInfo"]["name"] == "levain_gh" and lines[2]["error"]["code"] == mcp.PARSE_ERROR
    text = lines[1]["result"]["content"][0]["text"]
    assert text.startswith("HTTP 200") and TOKEN_L not in r.stdout + r.stderr and "[token]" in text


def test_git_fetch_scrubs_a_token_that_appears_in_a_branch_name(home, upstream):
    """Revert: the ``scrub`` on ``out`` at the end of ``git_fetch`` (a branch named like the token must not echo it)."""
    both(home)
    git("--git-dir", f"{upstream}levainhq/levain.git", "branch", TOKEN_L, "main")
    out = fetch(home, upstream)
    assert TOKEN_L not in out and "[token]" in out
