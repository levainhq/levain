"""S1 session 2b: ``levain.slot.render`` (CONTRACT §3.4, §5.3, §5.5, §8.1, §24). Every test is a TEXT or data test:
no profile is ever applied here (trg-069)."""
from __future__ import annotations

import hashlib
import itertools
import json
import pathlib
import urllib.request

import pytest

from levain.contracts import canon
from levain.host_guard import _LOOSENING_NONEMPTY, _LOOSENING_TRUE
from levain.slot import Refused
from levain.slot import render as rd
from levain.slot.ids import SlotId
from levain.slot.render import Contains, HookTemplate, RenderFacts
from levain.slot.runtime_grammar import GitIdentity, RenderBlob, VerifierStep, refusal_line

S = "s-0123456789abcdef"
VF_SLOT = "/private/var/folders/zy/hhq908f97y7g9v4yl18g49l00000lq"
VF_PAIR = ("/private/var/folders/ab/c1", "/private/var/folders/ab/d2")
ROUTES = {"anthropic": "brokered", "openai": "brokered", "github": "egress", "cc-sub": "subscription_passthrough",
          "codex-sub": "subscription_passthrough"}
REPO = pathlib.Path(__file__).resolve().parent.parent


def egress(host="darwin", vendor="claude", kind="interactive", auth="brokered", routes=None, index=3, **kw):
    slot = SlotId("egress", index)
    if routes is None:
        routes = {("brokered", "claude"): ("anthropic", "github"), ("brokered", "codex"): ("openai", "github"),
                  ("passthrough", "claude"): ("cc-sub", "github"),
                  ("passthrough", "codex"): ("codex-sub",)}[(auth, vendor)]
    blob_kw = {k: kw.pop(k) for k in list(kw) if k in ("model", "git_identity", "hooks", "allow_own_loopback",
                                                       "verifier_steps", "driver")}
    blob = RenderBlob(vendor=vendor, routes=tuple(routes), **blob_kw)
    facts = dict(host=host, session=S, slot=slot, kind=kind, vendor=vendor, auth=auth, slot_uid=3990,
                 slot_gid=3990, gwport=rd.gwport_for(host, slot), route_types=ROUTES, term="xterm-256color",
                 var_folders=(VF_SLOT,) if host == "darwin" else (),
                 # WF-1b: the base map gives every egress route its hosts (runtime_helper.base_routes)
                 route_hosts={"github": ("github.com", "api.github.com")})
    facts.update(kw)
    return blob, RenderFacts(**facts)


def verifier(host="darwin", vendor="claude", **kw):
    blob = RenderBlob(vendor=vendor, routes=(), verifier_steps=(VerifierStep(("pytest", "-q"), 60),),
                      driver="pytest-report")
    facts = dict(host=host, session=S, slot=SlotId("none", 1), kind="verifier", vendor=vendor, auth="none",
                 slot_uid=3980, slot_gid=3980, driver_uid=3981, driver_gid=3981,
                 var_folders=VF_PAIR if host == "darwin" else ())
    facts.update(kw)
    return blob, RenderFacts(**facts)


def refused(fn, *a, **kw) -> Refused:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value


def every_variant():
    """(host, vendor, class) x the options that change a profile or env."""
    for host, vendor in itertools.product(("darwin", "linux"), ("claude", "codex")):
        for auth, kind, loop, hooks in itertools.product(("brokered", "passthrough"),
                                                         ("interactive", "login", "dispatch", "planner"),
                                                         (None, False), ((), ("levain_queue",))):
            if hooks and vendor == "codex":
                continue
            tmpl = {h: HookTemplate(b"#!/bin/sh\n", "PreToolUse") for h in hooks}
            yield egress(host, vendor, kind, auth, allow_own_loopback=loop, hooks=frozenset(hooks),
                         hook_templates=tmpl)
        yield verifier(host, vendor)


# --- §5.3 unknown key ----------------------------------------------------------------------------------------

def test_render_unknown_key_refused():
    """runtime_grammar owns the unknown-key refusal; render's own stdin entry goes through it."""
    _, facts = egress()
    ok = {"format": "levain-session-render/1", "vendor": "claude", "routes": ["anthropic", "github"]}
    assert rd.render_from_stdin(canon.canonical_bytes(ok), facts).file("root/settings.json")
    for extra in ("driver_input", "sandbox", "permissions", "excludedCommands"):
        e = refused(rd.render_from_stdin, canon.canonical_bytes({**ok, extra: True}), facts)
        assert e.name == "unknown-key", extra


# --- §8 no syscall -------------------------------------------------------------------------------------------

def test_profile_has_no_syscall_rule():
    n = 0
    for blob, facts in every_variant():
        if facts.host != "darwin":
            continue
        text = rd.render_profile(blob, facts).decode()
        assert "syscall" not in text.lower()
        assert rd.render(blob, facts).file("root/profile.sb").data.decode() == text
        n += 1
    assert n >= 30


def test_renderer_refuses_its_own_syscall_output(monkeypatch):
    """The assertion lives IN the renderer: a profile that would name syscall (here via a poisoned allow-list) is
    never returned."""
    blob, facts = egress()
    monkeypatch.setattr(rd, "MACH_ALLOW", rd.MACH_ALLOW + ("syscall-unix",))
    assert refused(rd.render_profile, blob, facts).name == "profile-syscall-rule"
    monkeypatch.setattr(rd, "MACH_ALLOW", ("com.apple.SysCall.x",))
    assert refused(rd.render, blob, facts).name == "profile-syscall-rule"
    assert refused(rd.assert_no_syscall, ";; (allow syscall-mig)").name == "profile-syscall-rule"


def test_linux_has_no_profile():
    blob, facts = egress("linux")
    assert "root/profile.sb" not in [f.path for f in rd.render(blob, facts).files]
    assert refused(rd.render_profile, blob, facts).name == "unsupported-host"


# --- §3.4 env -----------------------------------------------------------------------------------------------

BASE_KEYS = {"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "CLAUDE_CODE_TMPDIR", "TMPPREFIX", "XDG_DATA_HOME",
             "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "npm_config_cache", "CLAUDE_CONFIG_DIR",
             "CODEX_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ATTR_NOSYSTEM", "DISABLE_AUTOUPDATER",
             "TERM"}
PROXY_KEYS = {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}


def test_vendor_env_exact():
    L = "/private/var/levain/live/" + S
    # macOS interactive brokered claude: base + proxy + base URL + own loopback (§24)
    blob, facts = egress()
    env = rd.render_env(blob, facts)["slot"]
    assert set(env) == BASE_KEYS | PROXY_KEYS | {"ANTHROPIC_BASE_URL", "LEVAIN_LOOPBACK_HOST", "HOST"}
    assert env["HOME"] == L and env["USER"] == env["LOGNAME"] == "_levain_s3"
    assert env["PATH"] == "/usr/local/lib/levain-vendor/bin:/usr/bin:/bin"
    assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"] == f"{L}/tmp" and env["TMPPREFIX"] == f"{L}/tmp/zsh"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:47103"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47103/r/anthropic"
    assert env["HOST"] == env["LEVAIN_LOOPBACK_HOST"] == "127.0.1.4"
    assert env["GIT_CONFIG_GLOBAL"] == f"{L}/root/gitconfig" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["TERM"] == "xterm-256color"
    # no handle is ever rendered (§3.2: start adds them at exec)
    assert rd.secret_env_names(blob, facts) == ("ANTHROPIC_AUTH_TOKEN",)
    assert not set(rd.secret_env_names(blob, facts)) & set(env)
    # dispatch: no own loopback; codex brokered: no ANTHROPIC_BASE_URL (its base URL is the provider block)
    blob, facts = egress(kind="dispatch", vendor="codex")
    env = rd.render_env(blob, facts)["slot"]
    assert set(env) == BASE_KEYS | PROXY_KEYS | {"SSL_CERT_FILE"}
    assert rd.secret_env_names(blob, facts) == ("OPENAI_API_KEY",)
    # p71 S8 D11: a claude dispatch executor gains the background-task switch, no other kind does
    blob, facts = egress(kind="dispatch")
    env = rd.render_env(blob, facts)["slot"]
    assert set(env) == BASE_KEYS | PROXY_KEYS | {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"}
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    # passthrough: no base URL, no token
    blob, facts = egress(auth="passthrough", kind="login")
    assert "ANTHROPIC_BASE_URL" not in rd.render_env(blob, facts)["slot"]
    assert rd.secret_env_names(blob, facts) == ()
    # queue grant: the socket path rendered, the cap added at exec
    blob, facts = egress(hooks=frozenset({"levain_queue"}),
                         hook_templates={"levain_queue": HookTemplate(b"x", "PreToolUse")})
    env = rd.render_env(blob, facts)["slot"]
    assert env["LEVAIN_QUEUE_SOCKET"] == "/private/var/run/levain/qb/slot3/q.sock"
    assert rd.secret_env_names(blob, facts) == ("ANTHROPIC_AUTH_TOKEN", "LEVAIN_QUEUE_CAP")
    # Linux: the netns forwarder's 47100, no own loopback alias, Linux prefix
    blob, facts = egress("linux")
    env = rd.render_env(blob, facts)["slot"]
    assert set(env) == BASE_KEYS | PROXY_KEYS | {"ANTHROPIC_BASE_URL"}
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:47100" and env["PATH"].startswith("/usr/lib/levain-vendor/bin:")
    # TERM is a parameter: absent = absent
    blob, facts = egress(term=None)
    assert "TERM" not in rd.render_env(blob, facts)["slot"]
    # none class: no proxy at all; driver vs child
    blob, facts = verifier()
    envs = rd.render_env(blob, facts)
    assert set(envs) == {"child", "driver"}
    assert set(envs["child"]) == BASE_KEYS - {"TERM"}
    assert envs["child"]["HOME"] == L and envs["child"]["USER"] == "_levain_c1"
    assert envs["driver"] == {"HOME": f"{L}/driver", "USER": "_levain_v1", "LOGNAME": "_levain_v1",
                              "PATH": envs["child"]["PATH"], "TMPDIR": f"{L}/driver"}
    assert rd.secret_env_names(blob, facts) == ()


def test_codex_on_macos_gets_the_os_ca_file():
    """R7 (r3c10-vm 10-30): with no SSL_CERT_FILE every Codex chatgpt.com tunnel broke after the server's
    certificate (rustls-native-certs could not reach securityd from the slot); with /etc/ssl/cert.pem a
    passthrough turn ran. Codex on macOS only, every kind and auth; never Claude, never Linux (unmeasured there)."""
    for kind, auth in itertools.product(("interactive", "login", "dispatch"), ("brokered", "passthrough")):
        blob, facts = egress(vendor="codex", kind=kind, auth=auth)
        assert rd.render_env(blob, facts)["slot"]["SSL_CERT_FILE"] == "/etc/ssl/cert.pem", (kind, auth)
        assert "SSL_CERT_FILE=/etc/ssl/cert.pem\n" in rd.render(blob, facts).file("root/env").data.decode()
        blob, facts = egress(vendor="claude", kind=kind, auth=auth)
        assert "SSL_CERT_FILE" not in rd.render_env(blob, facts)["slot"], (kind, auth)
    blob, facts = egress("linux", vendor="codex", auth="passthrough")
    assert "SSL_CERT_FILE" not in rd.render_env(blob, facts)["slot"]


def test_env_file_is_exactly_the_env():
    blob, facts = egress()
    r = rd.render(blob, facts)
    text = r.file("root/env").data.decode()
    lines = text.splitlines()
    assert lines == sorted(lines) and text.endswith("\n")
    assert dict(line.split("=", 1) for line in lines) == r.env["slot"]
    assert refused(rd.env_text, {"A": "x\ny"}).name == "bad-env"


def test_localhost_bypasses_proxy():
    """NO_PROXY: loopback (own dev servers and the brokered base URL) goes direct; anything else uses the proxy."""
    for kind in ("interactive", "dispatch"):
        for host in ("darwin", "linux"):
            blob, facts = egress(host, kind=kind)
            env = rd.render_env(blob, facts)["slot"]
            no = {"no": env["NO_PROXY"]}
            for h in ("localhost", "127.0.0.1", "::1"):
                assert urllib.request.proxy_bypass_environment(h, no), h
            base = urllib.request.urlparse(env["ANTHROPIC_BASE_URL"]).hostname
            assert urllib.request.proxy_bypass_environment(base, no)
            assert not urllib.request.proxy_bypass_environment("api.anthropic.com", no)
            assert not urllib.request.proxy_bypass_environment("github.com", no)
            if "HOST" in env:
                assert urllib.request.proxy_bypass_environment(env["HOST"], no)


# --- §5.3 pins + read-back -----------------------------------------------------------------------------------

def test_pins_regardless_of_input():
    tmpl = {"levain_gate": HookTemplate(b"#!/bin/sh\nexit 0\n", "PreToolUse", "Bash")}
    for host in ("darwin", "linux"):
        blob, facts = egress(host, model="claude-opus-5-5", hooks=frozenset(tmpl), hook_templates=tmpl)
        s = rd.render_cc_settings(blob, facts)
        assert s["permissions"]["disableBypassPermissionsMode"] == "disable"
        assert s["sandbox"]["excludedCommands"] == []
        assert s["sandbox"]["enabled"] is (host == "linux")
        if host == "linux":
            assert s["sandbox"]["failIfUnavailable"] is True
            assert s["sandbox"]["bwrapPath"] == "/usr/libexec/levain/bwrap"
            assert s["permissions"]["ask"] == ["Bash(dangerouslyDisableSandbox:true)"]
        else:
            assert "ask" not in s["permissions"] and "bwrapPath" not in s["sandbox"]
        assert s["model"] == "claude-opus-5-5"
        L = str(facts.S)
        assert s["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{L}/root/hooks/levain_gate", "onFailure": "block"}]}]}
        r = rd.render(blob, facts)
        hook = r.file("root/hooks/levain_gate")
        assert (hook.data, hook.uid, hook.gid, hook.mode) == (tmpl["levain_gate"].body, 0, 3990, 0o750)
    for host, vendor in itertools.product(("darwin", "linux"), ("claude", "codex")):
        blob, facts = egress(host, vendor)
        assert 'approval_policy = "never"' in rd.codex_toml(blob, facts).decode().splitlines()


def test_effective_config_readback():
    for host, vendor in itertools.product(("darwin", "linux"), ("claude", "codex")):
        blob, facts = egress(host, vendor)
        r = rd.render(blob, facts)
        exp = r.expected[vendor]
        observed = (json.loads(r.file("root/settings.json").data) if vendor == "claude"
                    else rd.codex_config(blob, facts)["top"])
        assert rd.readback_refusals(exp, observed) == []
        rd.refuse_readback(exp, observed)
        for key in exp:
            parts = key.split(".")
            bad = json.loads(json.dumps(observed))
            node = bad
            for p in parts[:-1]:
                node = node[p]
            node[parts[-1]] = "loosened"
            assert rd.readback_refusals(exp, bad) == [key]
            e = refused(rd.refuse_readback, exp, bad)
            assert e.name == "config-readback:" + rd.wire_name(key)
            assert refusal_line(e) == json.dumps({"ok": False, "refused": e.name}, separators=(",", ":")) + "\n"
            del node[parts[-1]]                                  # absent differs too
            assert rd.readback_refusals(exp, bad) == [key]
    blob, facts = egress("darwin")
    exp = rd.expected_effective_cc(blob, facts)
    good = json.loads(rd.render(blob, facts).file("root/settings.json").data)
    good["sandbox"]["enabled"] = 0                                # 0 is not false
    assert rd.readback_refusals(exp, good) == ["sandbox.enabled"]
    good["sandbox"]["enabled"] = False
    good["sandbox"]["excludedCommands"] = ["git"]                 # a merged array is a loosening
    assert rd.readback_refusals(exp, good) == ["sandbox.excludedCommands"]
    # Linux ask: a merge may ADD ask rules, never drop ours
    blob, facts = egress("linux")
    exp = rd.expected_effective_cc(blob, facts)
    assert isinstance(exp["permissions.ask"], Contains)
    good = json.loads(rd.render(blob, facts).file("root/settings.json").data)
    good["permissions"]["ask"] = ["Bash(rm:*)", "Bash(dangerouslyDisableSandbox:true)"]
    assert rd.readback_refusals(exp, good) == []
    good["permissions"]["ask"] = ["Bash(rm:*)"]
    assert rd.readback_refusals(exp, good) == ["permissions.ask"]


# --- WF-1a: WebFetch through the front (wfetch DESIGN §3 WF-1 items 1-3) -----------------------------------------

HOSTS = {"github": ("github.com", "api.github.com")}


def test_webfetch_pins_on_every_egress_session():
    """skipWebFetchPreflight on every egress session (RULINGS Q1 (b)), one ``WebFetch(domain:)`` allow per host of the
    session's egress routes (never the vendor route's), and the existing permission pins untouched."""
    for host, auth in itertools.product(("darwin", "linux"), ("brokered", "passthrough")):
        blob, facts = egress(host, "claude", auth=auth, route_hosts=HOSTS)
        s = rd.render_cc_settings(blob, facts)
        assert s["skipWebFetchPreflight"] is True
        assert s["disableClaudeAiConnectors"] is True
        assert s["permissions"]["allow"] == ["WebFetch(domain:api.github.com)", "WebFetch(domain:github.com)"]
        assert s["permissions"]["disableBypassPermissionsMode"] == "disable"
        if host == "linux":
            assert s["permissions"]["ask"] == ["Bash(dangerouslyDisableSandbox:true)"]
        assert not any("anthropic" in a for a in s["permissions"]["allow"])
    # no egress route in the session: the preflight is still skipped, no allow rule is rendered
    blob, facts = egress("darwin", "codex", auth="passthrough")
    s = rd.render_cc_settings(blob, facts)
    assert s["skipWebFetchPreflight"] is True and "allow" not in s["permissions"]
    # the none class has no network: none of it but the connectors pin
    blob, facts = verifier()
    s = rd.render_cc_settings(blob, facts)
    assert "skipWebFetchPreflight" not in s and "allow" not in s["permissions"]
    assert s["disableClaudeAiConnectors"] is True


def test_webfetch_hosts_only_from_egress_routes_and_checked():
    """A host list for a non-egress route renders nothing; a host the policy grammar refuses is refused before any
    byte (it would otherwise be spliced into a permission rule)."""
    blob, facts = egress("darwin", route_hosts={**HOSTS, "anthropic": ("api.anthropic.com",)})
    assert rd.render_cc_settings(blob, facts)["permissions"]["allow"] == [
        "WebFetch(domain:api.github.com)", "WebFetch(domain:github.com)"]
    for bad in ("GitHub.com", "*.github.com", "github.com)", "127.0.0.1", "0x7f.0x1", "localhost", ""):
        blob, facts = egress("darwin", route_hosts={"github": (bad,)})
        e = refused(rd.render, blob, facts)
        assert e.name == "bad-config", bad
    blob, facts = egress("darwin", route_hosts={"github": ()})
    assert refused(rd.render, blob, facts).name == "egress-route-no-hosts"     # WF-1b


def test_webfetch_pins_read_back():
    blob, facts = egress("darwin", route_hosts=HOSTS)
    exp = rd.expected_effective_cc(blob, facts)
    assert exp["skipWebFetchPreflight"] is True and exp["disableClaudeAiConnectors"] is True
    good = json.loads(rd.render(blob, facts).file("root/settings.json").data)
    assert rd.readback_refusals(exp, good) == []
    good["skipWebFetchPreflight"] = False
    assert rd.readback_refusals(exp, good) == ["skipWebFetchPreflight"]
    blob, facts = egress("darwin", "codex")
    assert rd.expected_effective_codex(blob, facts)["web_search"] == "disabled"


def test_codex_web_search_disabled():
    """RULINGS Q3: until Phill rules, Codex's server-side search is off (under danger-full-access it defaults to live,
    a vendor-side fetch the front cannot see)."""
    for host, vendor, auth in itertools.product(("darwin", "linux"), ("claude", "codex"), ("brokered", "passthrough")):
        blob, facts = egress(host, vendor, auth=auth)
        assert 'web_search = "disabled"' in rd.codex_toml(blob, facts).decode().splitlines()
    blob, facts = verifier(vendor="codex")
    assert 'web_search = "disabled"' in rd.codex_toml(blob, facts).decode().splitlines()


def test_codex_onboarding_and_provider():
    blob, facts = egress("darwin", "codex", model="gpt-6")
    r = rd.render(blob, facts)
    text = r.file("root/codex.toml").data
    assert r.file("config/codex/config.toml").data == text          # the one §5.5 accepts
    t = text.decode()
    assert '[model_providers.levain]' in t and 'base_url = "http://127.0.0.1:47103/r/openai/v1"' in t
    assert 'env_key = "OPENAI_API_KEY"' in t and 'model = "gpt-6"' in t
    assert f'[projects."/private/var/levain/live/{S}/repo"]' in t
    with pytest.raises(KeyError):
        r.file("config/claude/.claude.json")
    # claude session: no codex provider pointing at the claude route, no codex pre-seed
    blob, facts = egress("darwin", "claude")
    r = rd.render(blob, facts)
    assert "model_provider" not in r.file("root/codex.toml").data.decode()
    with pytest.raises(KeyError):
        r.file("config/codex/config.toml")
    seed = json.loads(r.file("config/claude/.claude.json").data)
    assert seed["hasCompletedOnboarding"] is True
    assert seed["projects"][f"/private/var/levain/live/{S}/repo"]["hasTrustDialogAccepted"] is True
    assert not any("token" in k.lower() or "key" in k.lower() for k in seed)


def test_file_rows_owner_mode():
    blob, facts = egress()
    for f in rd.render(blob, facts).files:
        if f.path.startswith("root/"):
            assert (f.uid, f.gid, f.mode) == (0, 3990, 0o640), f.path
        else:
            assert (f.uid, f.gid, f.mode) == (3990, 3990, 0o600), f.path
    blob, facts = verifier()
    for f in rd.render(blob, facts).files:
        if f.path.startswith("root/"):
            assert (f.uid, f.gid, f.mode) == (0, 0, 0o644), f.path
        else:
            assert (f.uid, f.gid) == (3980, 3980), f.path


def test_gitconfig_quotes_every_value():
    blob, facts = egress(git_identity=GitIdentity('A "b" \\ #c; d', "a@b.c"))
    assert rd.render(blob, facts).file("root/gitconfig").data == \
        b'[user]\n\tname = "A \\"b\\" \\\\ #c; d"\n\temail = "a@b.c"\n'
    blob, facts = egress()
    assert rd.render(blob, facts).file("root/gitconfig").data == b""


# --- refusals on the (blob, facts) pair ----------------------------------------------------------------------

def test_check_facts_refusals():
    cases = [
        (egress(gwport=47100), "gwport-mismatch"),
        (egress("linux", gwport=47103), "gwport-mismatch"),
        (egress(routes=("anthropic", "nope")), "route-unknown"),
        (egress(routes=("github",)), "auth-routes-mismatch"),
        (egress(routes=("anthropic", "cc-sub")), "auth-routes-mismatch"),
        (egress(routes=("anthropic", "openai")), "routes-per-vendor"),
        (egress(auth="passthrough", routes=("anthropic",)), "auth-routes-mismatch"),
        (egress(kind="dispatch", allow_own_loopback=True), "own-loopback-not-allowed"),
        (egress("linux", allow_own_loopback=True), "own-loopback-not-allowed"),
        (egress(vendor="codex", hooks=frozenset({"levain_gate"}),
                hook_templates={"levain_gate": HookTemplate(b"x", "PreToolUse")}), "hooks-not-wired:codex"),
        (egress(hooks=frozenset({"levain_gate"})), "hook-template-missing"),
        (egress(var_folders=("/private/var/folders/../etc",)), "bad-config"),
        (egress(var_folders=('/private/var/folders/a"/b',)), "bad-config"),
        (egress(var_folders=()), "bad-config"),
        (egress("linux", var_folders=(VF_SLOT,)), "bad-config"),
        (egress(term="xterm\nX=1"), "bad-config"),
        (egress(kind="verifier"), "class-kind-mismatch"),
        (egress(slot_uid=0), "bad-config"),
        (verifier(auth="brokered"), "class-auth-mismatch"),
        (verifier(var_folders=(VF_SLOT,)), "bad-config"),
        (verifier(gwport=47101), "gwport-mismatch"),
    ]
    blob, facts = egress()
    cases.append(((RenderBlob(vendor="codex", routes=blob.routes), facts), "vendor-mismatch"))
    cases.append(((RenderBlob(vendor="claude", routes=blob.routes, driver="d",
                              verifier_steps=(VerifierStep(("x",), 1),)), facts), "verifier-keys-unexpected"))
    vb, vf = verifier()
    cases.append(((RenderBlob(vendor="claude", routes=("github",), driver="d",
                              verifier_steps=vb.verifier_steps), vf), "routes-not-empty"))
    cases.append(((RenderBlob(vendor="claude", routes=()), vf), "verifier-keys-missing"))
    cases.append(((RenderBlob(vendor="claude", routes=(), driver="d", verifier_steps=vb.verifier_steps,
                              hooks=frozenset({"levain_queue"})), vf), "hooks-not-allowed"))
    for (b, f), name in cases:
        assert refused(rd.render, b, f).name == name, name


def test_own_loopback_blob_may_turn_it_off():
    blob, facts = egress(allow_own_loopback=False)
    r = rd.render(blob, facts)
    assert not r.own_loopback and "HOST" not in r.env["slot"]
    p = r.file("root/profile.sb").data.decode()
    assert '"localhost:*"' not in p and '(remote ip "localhost:47103")' in p
    blob, facts = egress(kind="login")
    assert rd.render(blob, facts).own_loopback


# --- §5.5 discovery -------------------------------------------------------------------------------------------

#: CONTRACT §5.5, copied here on purpose so a name dropped from the module's table fails a test.
CONTRACT_5_5 = {
    "claude": {"settings.json", "settings.local.json", "CLAUDE.md", "AGENTS.md", "rules", "skills", "agents",
               "commands", "output-styles", "plugins", "cowork_settings.json", "daemon.json", "daemon"},
    "codex": {"config.toml", ".env", "AGENTS.md", "AGENTS.override.md", "hooks.json", "rules", "agents",
              "environments.toml"},
    "home": {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", ".gitconfig", ".config/git",
             ".config/anthropic", ".agents", ".netrc", ".bash_profile", ".bash_login"},
}


#: Head ruling (S2 fix round 2, comp r1 7): HOME's two .config names also where the env's XDG_CONFIG_HOME points.
XDG_5_5 = {"xdg/config/git", "xdg/config/anthropic"}


def test_planted_discovery_refused_by_name():
    assert {r: set(n) for r, n in rd.DISCOVERY.items()} == {**CONTRACT_5_5, "home": CONTRACT_5_5["home"] | XDG_5_5}
    assert rd.discovery_refusals({"claude": [".claude.json", "backups/", "sessions"], "codex": ["auth.json"],
                                  "home": ["root", "repo", "config", "xdg", "tmp", "sock", "in"]}) == []
    for root, names in rd.DISCOVERY.items():
        for name in names:
            for spelled in (name, name + "/", name + "/inner"):
                bad = rd.discovery_refusals({root: [spelled]})
                assert [e.name for e in bad] == [f"planted-discovery:{rd.wire_name(root + '-' + name)}"], spelled
                assert refusal_line(bad[0]) != '{"ok":false,"refused":"internal"}\n'
    e = refused(rd.refuse_discovery, {"claude": ["settings.local.json"]})
    assert e.name == "planted-discovery:claude-settings-local-json"
    # config.toml under CODEX_HOME: accepted only as the bytes render made
    blob, facts = egress(vendor="codex")
    mine = rd.render(blob, facts).file("config/codex/config.toml").data
    assert rd.discovery_refusals({"codex": ["config.toml"]}, codex_config=mine, rendered_codex_config=mine) == []
    assert [e.name for e in rd.discovery_refusals({"codex": ["config.toml"]}, codex_config=mine + b"x=1\n",
                                                  rendered_codex_config=mine)] == ["planted-discovery:codex-config-toml"]
    assert len(rd.discovery_refusals({"codex": ["config.toml"]})) == 1
    assert len(rd.discovery_refusals({"codex": ["config.toml/x"]}, codex_config=mine,
                                     rendered_codex_config=mine)) == 1
    # every planted name at once: one refusal each
    allp = {r: list(n) for r, n in rd.DISCOVERY.items()}
    assert len(rd.discovery_refusals(allp)) == sum(len(n) for n in rd.DISCOVERY.values())


def test_planted_discovery_folds_case_and_unicode():
    """PRE-L3 4-2 (complement MED): on a case- and normalization-insensitive store (APFS, the Mac slots) the vendor's
    ``open("settings.json")`` resolves to a planted ``Settings.JSON``, so every spelling that folds to a §5.5 name is
    refused, by the one fold the settings check and the dispatch workspace use (NFD, then casefold)."""
    import unicodedata
    for root, spelled, name in (("claude", "Settings.JSON", "settings.json"), ("claude", "claude.md", "CLAUDE.md"),
                                ("claude", "SKILLS/x", "skills"), ("home", ".Config/Git", ".config/git"),
                                ("codex", "Config.TOML", "config.toml")):
        got = rd.discovery_refusals({root: [spelled]})
        assert [e.name for e in got] == [f"planted-discovery:{rd.wire_name(root + '-' + name)}"], spelled
    # an NFD-decomposed spelling folds to the same name as its NFC twin
    nfd = unicodedata.normalize("NFD", "settings.json")
    assert [e.name for e in rd.discovery_refusals({"claude": [nfd.upper()]})] == ["planted-discovery:claude-settings-json"]
    assert rd.fold_name("Ｓ") == rd.fold_name("ｓ")
    assert rd.fold_name("É") == rd.fold_name(unicodedata.normalize("NFD", "é"))
    # the codex config.toml exemption holds only for the exact rendered spelling, alone
    blob, facts = egress(vendor="codex")
    mine = rd.render(blob, facts).file("config/codex/config.toml").data
    assert rd.discovery_refusals({"codex": ["config.toml"]}, codex_config=mine, rendered_codex_config=mine) == []
    for listing in (["Config.TOML"], ["config.toml", "CONFIG.toml"]):
        assert [e.name for e in rd.discovery_refusals({"codex": listing}, codex_config=mine,
                                                      rendered_codex_config=mine)] == \
            ["planted-discovery:codex-config-toml"], listing
    # the repo-level listing (adopt preview) folds the same way
    assert rd.repo_discovery_names([".Claude/settings.json", "agents.md"]) == [".claude", "AGENTS.md"]


def test_repo_level_names_not_refused_but_listed():
    repo = [".claude/settings.json", ".codex", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".agents/x", "src"]
    assert rd.discovery_refusals({}) == []
    assert rd.repo_discovery_names(repo) == [".claude", ".codex", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".agents"]
    assert rd.repo_discovery_names(["src", "README.md"]) == []


# --- repo-layer loosening ------------------------------------------------------------------------------------

def _nest(keys, value):
    d = value
    for k in reversed(keys):
        d = {k: d}
    return d


def test_loosening_repo_layer_refused_by_key():
    for f in (".claude/settings.json", ".claude/settings.local.json"):
        for keys in _LOOSENING_TRUE:
            got = rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, True)}})
            assert [e.name for e in got] == ["loosening:" + rd.wire_name("sandbox." + ".".join(keys))]
            assert f in got[0].detail
            assert rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, False)}}) == []
        for keys in _LOOSENING_NONEMPTY:
            got = rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, ["x"])}})
            assert [e.name for e in got] == ["loosening:" + rd.wire_name("sandbox." + ".".join(keys))]
            assert rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, [])}}) == []
    e = refused(rd.refuse_repo_loosening, {".claude/settings.local.json": {"sandbox": {"excludedCommands": ["*"]}}})
    assert e.name == "loosening:sandbox-excluded-commands"
    assert refusal_line(e) == '{"ok":false,"refused":"loosening:sandbox-excluded-commands"}\n'
    assert [x.name for x in rd.repo_loosening_refusals({"a": []})] == ["repo-settings-malformed"]
    assert [x.name for x in rd.repo_loosening_refusals({"a": {"sandbox": 1}})] == ["repo-settings-malformed"]


@pytest.mark.parametrize("bad", [False, "", 0, None, {}, [1], ["x", None]], ids=repr)
def test_loosening_list_key_of_the_wrong_type_refused_by_key(bad):
    """RULINGS 1026-10 Q5: the repo layer is repo-author controlled, and a falsy non-list passed the not-empty check;
    Claude Code expects a list of strings, and a schema type error can drop the sandbox block silently (host_guard's
    managed check refuses the same way, L3 r1 codex 2)."""
    f = ".claude/settings.json"
    for keys in _LOOSENING_NONEMPTY:
        got = rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, bad)}})
        assert [e.name for e in got] == ["loosening:" + rd.wire_name("sandbox." + ".".join(keys))], keys
        assert "must be a list of strings" in got[0].detail
        assert rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, [])}}) == []


@pytest.mark.parametrize("bad", [0, 1, "", "true", None, [], {}], ids=repr)
def test_loosening_bool_key_of_the_wrong_type_refused_by_key(bad):
    """RULINGS 1026-10 Q5, the bool half: only a JSON boolean is vetted; ``"forbid"`` for a boolean key made CC drop
    the block silently (host_guard's _SANDBOX_TYPES note)."""
    f = ".claude/settings.json"
    for keys in _LOOSENING_TRUE:
        got = rd.repo_loosening_refusals({f: {"sandbox": _nest(keys, bad)}})
        assert [e.name for e in got] == ["loosening:" + rd.wire_name("sandbox." + ".".join(keys))], keys
        assert "must be bool" in got[0].detail


def test_loosening_sandbox_subtable_that_is_not_an_object_is_malformed():
    for sub in ("network", "filesystem"):
        for bad in (1, "", None, [], False):
            got = rd.repo_loosening_refusals({"a": {"sandbox": {sub: bad}}})
            assert [(x.name, x.detail) for x in got] == [("repo-settings-malformed", f"a: sandbox.{sub}")]
        assert rd.repo_loosening_refusals({"a": {"sandbox": {sub: {}}}}) == []


def test_repo_settings_env_naming_the_queue_refused():
    """From the run (levain-memory 1019-s1design/runs/s3-queue-env/RUN.txt): a committed settings ``env`` naming the
    queue's socket or cap launched, and the consent hook then denies every gated call; now refused by name."""
    f = ".claude/settings.json"
    got = rd.repo_loosening_refusals({f: {"env": {rd.QUEUE_SOCKET_ENV: "/tmp/x.sock", "FOO": "1"}},
                                      ".claude/settings.local.json": {"env": {rd.QUEUE_CAP_ENV: "x"}}})
    assert [x.name for x in got] == ["loosening:env-levain-queue-socket", "loosening:env-levain-queue-cap"]   # by file
    assert rd.repo_loosening_refusals({f: {"env": {"FOO": "1", "LEVAIN_QUEUEX": "y"}}}) == []
    assert [x.name for x in rd.repo_loosening_refusals({f: {"env": ["x"]}})] == ["repo-settings-malformed"]


def test_repo_settings_env_naming_a_proxy_var_refused():
    """WF-1 item 2: a repo layer clearing or redirecting the proxy vars cannot escape the wall, but every fetch would
    then fail with no cause shown; refused at launch by name instead."""
    f = ".claude/settings.json"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY",
                 "all_proxy"):
        got = rd.repo_loosening_refusals({f: {"env": {name: ""}}})
        assert [x.name for x in got] == ["loosening:" + rd.wire_name("env." + name)], name
        assert f in got[0].detail
    assert rd.repo_loosening_refusals({f: {"env": {"PROXY_HOST": "x", "HTTPS_PROXYX": "y"}}}) == []


def test_repo_settings_env_moving_the_billing_identity_refused():
    """PRE-L3 4-1 (codex HIGH): a repo settings ``env`` naming a key or bearer token Claude Code prefers to the stored
    login (:data:`ABSENT_ENV` / :data:`PASSTHROUGH_ABSENT_ENV`) moves the bill of an unattended run (or stops it at the
    "use this API key?" prompt), and a ``*_BASE_URL`` moves where its traffic goes; each is refused by name."""
    f = ".claude/settings.json"
    # class 1: ABSENT_ENV (claude)
    for name in rd.ABSENT_ENV["claude"]:
        got = rd.repo_loosening_refusals({f: {"env": {name: "sk-x"}}})
        assert [x.name for x in got] == ["loosening:" + rd.wire_name("env." + name)], name
        assert f in got[0].detail
    # class 2: PASSTHROUGH_ABSENT_ENV (claude), the bearer token included
    assert "ANTHROPIC_AUTH_TOKEN" in rd.PASSTHROUGH_ABSENT_ENV["claude"]
    for name in rd.PASSTHROUGH_ABSENT_ENV["claude"]:
        got = rd.repo_loosening_refusals({".claude/settings.local.json": {"env": {name: "t"}}})
        assert [x.name for x in got] == ["loosening:" + rd.wire_name("env." + name)], name
    # class 3: any *_BASE_URL
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "OPENAI_BASE_URL"):
        got = rd.repo_loosening_refusals({f: {"env": {name: "http://x"}}})
        assert [x.name for x in got] == ["loosening:" + rd.wire_name("env." + name)], name
    e = refused(rd.refuse_repo_loosening, {f: {"env": {"ANTHROPIC_AUTH_TOKEN": "t"}}})
    assert refusal_line(e) == '{"ok":false,"refused":"loosening:env-anthropic-auth-token"}\n'
    # controls: a name that only contains the suffix, a model id and harmless toggles still launch (a bare BASE_URL
    # no longer does: the _URL suffix refuses it, pf4r3)
    assert rd.repo_loosening_refusals({f: {"env": {"MY_BASE_URL_NOTE": "x", "ANTHROPIC_MODEL": "z",
                                                   "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000",
                                                   "DISABLE_TELEMETRY": "1"}}}) == []


def _env_refusal(name: str):
    return rd.repo_loosening_refusals({".claude/settings.json": {"env": {name: "x"}}})


def test_repo_env_vendor_namespace_is_default_deny():
    """pf4r3 (L3 r2 codex OPEN + MED): inside the vendor's namespace (every name its env-var reference documents, and
    any name starting ``CLAUDE_``, ``ANTHROPIC_``, ``BETA_``, ``OTEL_``, ``DISABLE_`` or ``MCP_``, and the runtime's TLS
    and options names) a repo settings ``env`` name is refused unless the allowlist names it; outside it, the provider
    prefixes and the credential or endpoint suffixes are refused and every other name launches. A deny list of name
    families missed vendor names each round while refusing harmless ones; the allowlist inverts that."""
    from levain import sandbox_keys as sk
    deny = "not on the repo env allowlist"
    cases = {
        # L3 r2 codex: a messaging socket, its token, a tracing endpoint
        "CLAUDE_CODE_MESSAGING_SOCKET": deny, "CLAUDE_CODE_MESSAGING_TOKEN": deny, "BETA_TRACING_ENDPOINT": deny,
        # invented: a vendor name the reference does not have yet
        "CLAUDE_CODE_NEW_THING_2027": deny, "ANTHROPIC_NEW_ROUTE": deny, "MCP_NEW_ROUTE": deny,
        "OTEL_EXPORTER_OTLP_ENDPOINT": deny, "DISABLE_NEW_GUARD": deny, "BETA_NEW_THING": deny,
        # credentials, endpoints, provider switches, tenants, TLS, config and code loading
        "ANTHROPIC_API_KEY": deny, "ANTHROPIC_AWS_WORKSPACE_ID": deny, "ANTHROPIC_BETAS": deny,
        "CLAUDE_CODE_USE_BEDROCK": deny, "CLAUDE_CODE_OAUTH_TOKEN": deny, "CLAUDE_CODE_CERT_STORE": deny,
        "CLAUDE_CONFIG_DIR": deny, "CLAUDE_CODE_PLUGIN_DIRS": deny, "CLAUDE_ENV_FILE": deny,
        "CLAUDE_CODE_SHELL_PREFIX": deny, "NODE_OPTIONS": deny, "NODE_EXTRA_CA_CERTS": deny,
        "NODE_TLS_REJECT_UNAUTHORIZED": deny, "SSL_CERT_FILE": deny, "SSL_CERT_DIR": deny, "REQUESTS_CA_BUNDLE": deny,
        # names Claude Code sets for its own subprocesses, which the consent band reads to know the session
        "CLAUDE_CODE_SESSION_ID": deny, "CLAUDECODE": deny, "CLAUDE_PID": deny, "CLAUDE_CODE_ENTRYPOINT": deny,
        # a hook-off mode, a safety prompt removed, a tool added, and names levain renders for the slot
        "CLAUDE_CODE_SAFE_MODE": deny, "CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT": deny,
        "CLAUDE_CODE_USE_POWERSHELL_TOOL": deny, "DISABLE_AUTOUPDATER": deny,
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": deny,
        # L3 r3 complement: the auto-mode permission classifier's model, and prompt text sent to Claude on resume
        "CLAUDE_CODE_AUTO_MODE_MODEL": deny, "CLAUDE_CODE_RESUME_PROMPT": deny,
        # row 168: the vendor's permission-modes page names this variable as choosing the classifier model too
        "ANTHROPIC_DEFAULT_SONNET_MODEL": deny,
        # outside the namespace: a provider prefix, or a credential or endpoint suffix on any stem
        "AWS_PROFILE": "prefix AWS_", "GOOGLE_APPLICATION_CREDENTIALS": "prefix GOOGLE_",
        "VERTEX_REGION_NEXT_MODEL": "prefix VERTEX_", "OPENAI_ORG_ID": "prefix OPENAI_",
        "MYAPP_API_KEY": "suffix _API_KEY", "MYAPP_ENDPOINT": "suffix _ENDPOINT", "MYAPP_TOKEN": "suffix _TOKEN",
        "MYAPP_SOCKET": "suffix _SOCKET", "MYAPP_URL": "suffix _URL", "MYAPP_SECRET": "suffix _SECRET",
        "MYAPP_PROFILE": "suffix _PROFILE",
    }
    for name, rule in cases.items():
        assert sk.repo_env_rule(name) == rule, name
        for spelled in (name, name.lower()):          # compared upper-cased
            got = _env_refusal(spelled)
            # the name as _refused caps it (refusal_line's 64)
            assert [x.name for x in got] == [rd._refused("loosening", "env." + spelled, "").name], spelled
            assert spelled in got[0].detail and rule in got[0].detail, got[0].detail
    # codex's r1 AWS bypass, whole: every name refused, once
    doc = {"env": {"CLAUDE_CODE_USE_ANTHROPIC_AWS": "1", "ANTHROPIC_AWS_API_KEY": "repo-key",
                   "ANTHROPIC_AWS_WORKSPACE_ID": "repo-workspace"}}
    assert sorted(x.name for x in rd.repo_loosening_refusals({".claude/settings.json": doc})) == [
        "loosening:env-anthropic-aws-api-key", "loosening:env-anthropic-aws-workspace-id",
        "loosening:env-claude-code-use-anthropic-aws"]
    # model choice, caps, timeouts, off switches, and a project's own names launch
    for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                 "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL",
                 "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "MAX_THINKING_TOKENS", "BASH_DEFAULT_TIMEOUT_MS",
                 "DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                 "DEBUG", "MYAPP_MODE", "APP_ENV", "MY_BASE_URL_NOTE", "API_KEY_NOTE", "anthropic_model"):
        assert sk.repo_env_rule(name) is None, name
        assert _env_refusal(name) == [], name
    # the proxy names keep their own refusal, once
    assert [x.name for x in _env_refusal("HTTPS_PROXY")] == ["loosening:env-https-proxy"]
    assert [x.name for x in _env_refusal("https_proxy")] == ["loosening:env-https-proxy"]


#: Words whose presence in a variable's doc row would put it outside the allowlist's ground (an endpoint, a credential,
#: an identity, TLS trust, telemetry headers, code loading, the sandbox or permission profile).
_ENV_DOC_FORBIDDEN = ("url", "endpoint", "host", "socket", "token", "key", "auth", "login", "credential", "cert",
                      "proxy", "provider", "account", "org", "workspace", "project", "tenant", "header", "helper",
                      "wrapper", "dir", "path", "plugin", "sandbox", "permission")
#: Allowlisted names whose doc row carries one of those words only incidentally (a mention of a provider the
#: variable does not select, a "project settings" scope note, output "tokens"), with the words; each is justified
#: by name in the pf4r3 RETURN. A row gaining a word fails until it is judged again.
_ENV_DOC_JUSTIFIED = {
    "ANTHROPIC_DEFAULT_FABLE_MODEL": ('provider',),
    "API_FORCE_IDLE_TIMEOUT": ('provider',),
    "API_TIMEOUT_MS": ('header', 'proxy'),
    "CLAUDE_AX_PREPARK_MS": ('project',),
    "CLAUDE_AX_SCREEN_READER": ('project',),
    "CLAUDE_AX_STARTUP_QUIET_MS": ('project',),
    "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": ('provider',),
    "CLAUDE_CODE_ATTRIBUTION_HEADER": ('credential', 'provider', 'token', 'url'),
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": ('token',),
    "CLAUDE_CODE_BASH_EDIT_DIFF": ('permission',),
    "CLAUDE_CODE_CONNECT_TIMEOUT_MS": ('header',),
    "CLAUDE_CODE_DISABLE_ARTIFACT": ('key',),
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": ('project',),
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": ('plugin',),
    "CLAUDE_CODE_DISABLE_CFC_PROMPT": ('host',),
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": ('project',),
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": ('header', 'proxy'),
    "CLAUDE_CODE_DISABLE_FILE_MENTIONS": ('path', 'project'),
    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": ('account',),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": ('plugin',),
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": ('proxy',),
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": ('plugin',),
    "CLAUDE_CODE_DISABLE_THINKING": ('provider',),
    "CLAUDE_CODE_ENABLE_AUTO_MODE": ('provider',),
    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": ('proxy',),
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": ('account',),
    "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": ('token',),
    "CLAUDE_CODE_GZIP_REQUEST_BODIES": ('proxy',),
    "CLAUDE_CODE_HIDE_CWD": ('path',),
    "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": ('token',),
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": ('token',),
    "CLAUDE_CODE_MAX_RETRIES": ('key',),
    "CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS": ('plugin',),
    "CLAUDE_CODE_PROMPT_CACHE_TTL": ('helper',),
    "CLAUDE_CODE_SCRIPT_CAPS": ('key',),
    "CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS": ('host', 'permission', 'project'),
    "CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS": ('url',),
    "CLAUDE_ENABLE_STREAM_WATCHDOG": ('provider',),
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": ('proxy',),
    "DEBUG": ('path',),
    "DISABLE_FEEDBACK_COMMAND": ('path',),
    "DISABLE_INSTALL_GITHUB_APP_COMMAND": ('provider',),
    "DISABLE_INTERLEAVED_THINKING": ('header', 'provider'),
    "DISABLE_LOGIN_COMMAND": ('key', 'login'),
    "DISABLE_TELEMETRY": ('path',),
    "ENABLE_PROMPT_CACHING_1H": ('key',),
    "FORCE_HYPERLINK": ('url',),
    "IS_DEMO": ('header',),
    "MAX_MCP_OUTPUT_TOKENS": ('token',),
    "MAX_THINKING_TOKENS": ('provider', 'token'),
    "MCP_DISCOVERY_CACHE": ('account',),
}


def _env_doc_words(row: str) -> tuple:
    import re
    prose = re.sub(r"\]\([^)]*\)", "]", row).lower()          # link targets are not the variable's meaning
    return tuple(sorted(w for w in _ENV_DOC_FORBIDDEN if re.search(r"\b" + w + r"s?\b", prose)))


def test_repo_env_rule_over_the_vendor_reference():
    """pf4r3: every variable in the vendor's env-var reference (the fixture: the table's names and doc rows, fetched on
    the date it names) gets a verdict, and the recorded verdicts are the rule's. The allowlist is drawn from it: a name
    is on it only if its doc row changes no endpoint, provider, credential, identity, TLS trust, telemetry destination
    or headers, code loading or sandbox profile, and no allowlisted row names a forbidden word unless judged."""
    from levain import sandbox_keys as sk
    from levain.claude_code_env_names import FETCHED, VENDOR_ENV_NAMES
    table = json.loads((REPO / "tests/fixtures/claude_code_env_vars_1108.json").read_text())
    assert table["fetched"] == FETCHED == "2026-11-08"
    assert table["source"].startswith("https://code.claude.com/docs/en/env-vars")
    assert set(table["verdicts"]) == set(table["rows"]) == VENDOR_ENV_NAMES
    got = {n: ("refused" if sk.repo_env_rule(n) else "allowed") for n in VENDOR_ENV_NAMES}
    assert got == table["verdicts"]
    allowed = {n for n, v in got.items() if v == "allowed"}
    assert allowed == set(sk.REPO_ENV_ALLOW)
    assert all(sk.repo_env_rule(n) == "not on the repo env allowlist" for n in VENDOR_ENV_NAMES - allowed)
    words = {n: _env_doc_words(table["rows"][n]) for n in sorted(allowed)}
    assert {n: w for n, w in words.items() if w} == _ENV_DOC_JUSTIFIED
    for must in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "BETA_TRACING_ENDPOINT",
                 "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_MANTLE",
                 "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CERT_STORE", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
                 "HTTPS_PROXY", "CLAUDE_CODE_IDE_HOST_OVERRIDE", "CLAUDE_CODE_GATEWAY_HINT_HEADERS"):
        assert table["verdicts"][must] == "refused", must
    for must in ("ANTHROPIC_MODEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                 "DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DEBUG"):
        assert table["verdicts"][must] == "allowed", must
    assert table["verdicts"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "refused"


def test_vendor_env_names_module_is_stdlib_data():
    """pf4r3: the runtime reads the vendor's names from a module under ``levain/`` that imports nothing (it runs in the
    3.9 run server, beside ``levain.sandbox_keys``), never from ``tests/``."""
    import ast
    from levain import claude_code_env_names as cen
    from levain import sandbox_keys as sk
    tree = ast.parse(pathlib.Path(cen.__file__).read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [(i.module, [a.name for a in i.names]) for i in imports] == [("__future__", ["annotations"])]
    assert "tests/" not in pathlib.Path(sk.__file__).read_text()
    assert not hasattr(sk, "REPO_ENV_SWITCH_PREFIX") and not hasattr(sk, "REPO_ENV_CLAUDE_WORDS")


def test_the_credential_name_lists_are_refused_by_the_repo_env_rule():
    """The render's absent-env lists and the two carry lists (a room child's kept names, a resumed session's carried
    names) each name only variables the repo-env rule refuses, so a name added to one of them that the rule lets
    through fails here."""
    from levain import sandbox_keys as sk
    from levain.consent import delivery as dv
    from levain.desk import room_launch as rl
    assert not hasattr(rd, "_REPO_BILLING_ENV") and not hasattr(sk, "CLAUDE_CREDENTIAL_ENV")
    names = set(dv._AGENT_AUTH) | {n for n in rl.ENV_KEEP if n.startswith("CLAUDE_")}
    for v in ("claude",):
        names |= rd.ABSENT_ENV[v] | rd.PASSTHROUGH_ABSENT_ENV[v]
    assert names and all(sk.repo_env_rule(n) for n in names), sorted(n for n in names if not sk.repo_env_rule(n))


def test_repo_settings_credential_helper_keys_refused():
    """PRE-L3 fix4 (L1 MED-2, L2 MED-2), pf4r2 R2 (codex MED): a repo settings KEY that runs a command to supply a
    credential or request headers is refused at the repo layer, by key pattern (case-insensitive), whatever its
    value: a ``*Helper``, a ``*AuthRefresh`` or a ``*CredentialExport``."""
    from levain import sandbox_keys as sk
    cases = {"apiKeyHelper": "suffix helper", "otelHeadersHelper": "suffix helper",
             "awsAuthRefresh": "suffix authrefresh", "gcpAuthRefresh": "suffix authrefresh",
             "azureAuthRefresh": "suffix authrefresh",                      # invented: the family, not the list
             "awsCredentialExport": "suffix credentialexport", "gcpCredentialExport": "suffix credentialexport",
             "APIKEYHELPER": "suffix helper", "GcpAuthRefresh": "suffix authrefresh"}
    for key, rule in cases.items():
        assert sk.repo_settings_rule(key) == rule, key
        for path in (".claude/settings.json", ".claude/settings.local.json"):
            got = rd.repo_loosening_refusals({path: {key: "/bin/sh -c x"}})
            assert [x.name for x in got] == ["loosening:" + rd.wire_name(key)], key
            assert key in got[0].detail and path in got[0].detail and rule in got[0].detail
    for key in ("hooks", "statusLine", "model", "permissions", "forceLoginMethod", "helperText"):
        assert sk.repo_settings_rule(key) is None, key
    assert rd.repo_loosening_refusals({".claude/settings.json": {"hooks": {}, "statusLine": {}}}) == []


def test_repo_settings_rule_over_the_vendor_reference():
    """pf4r2 R2: every top-level settings key the vendor's settings reference lets a repo file set (scope ``Any
    file``, ``Project`` or ``Local``; the index fetched on the date the fixture names) gets the verdict recorded for
    it. Refused: the keys documented as running a command that supplies a credential or headers."""
    from levain import sandbox_keys as sk
    table = json.loads((REPO / "tests/fixtures/claude_code_settings_1108.json").read_text())
    assert table["fetched"] == "2026-11-08"
    assert table["source"].startswith("https://code.claude.com/docs/en/settings-reference")
    got = {k: ("refused" if sk.repo_settings_rule(k) else "allowed") for k in table["verdicts"]}
    assert got == table["verdicts"]
    assert sorted(k for k, v in table["verdicts"].items() if v == "refused") == [
        "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "otelHeadersHelper"]


def test_supervisor_has_no_second_fold():
    """PRE-L3 fix4 (L1 LOW-1): the settings reader folds names with render's ``fold_name``, not a copy of it; and
    that fold treats case, unicode compatibility, decomposition and whitespace as ruled."""
    import unicodedata
    from levain.slot import supervisor as sv
    assert not hasattr(sv, "_fold")
    assert rd.fold_name("Settings.JSON") == rd.fold_name("settings.json")
    assert rd.fold_name("ＣＬＡＵＤＥ.md") != rd.fold_name("claude.md")      # NFD, not NFKC: width forms stay distinct
    assert rd.fold_name(unicodedata.normalize("NFD", "é")) == rd.fold_name("É")
    assert rd.fold_name(" .claude") != rd.fold_name(".claude")              # whitespace is part of the name


def test_fold_name_is_nfd_after_casefold():
    """pf4r2 R3 (L3 r1 complement LOW): the fold is Unicode's canonical caseless form, NFD(casefold(NFD(x))), so its
    output is NFD whatever casefold returns. Measured on 2026-11-08: under the Unicode tables of CPython 3.12
    (15.0.0) and 3.9 (13.0.0) no single code point, and no pair of a case-changing character with a combining mark,
    gives a non-NFD casefold of its NFD, so the outer NFD changes nothing today; it is the guard for a table where it
    would. The characters below are the ones whose casefold expands or decomposes."""
    import unicodedata
    def canonical(x: str) -> str:
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", x).casefold())
    for c in ("ΐ", "ﬃ", "İ", "ᾷ", "ẞ", "ͅ", "ᾀ", "ŉ", "ǰ"):
        assert rd.fold_name(c) == canonical(c), ascii(c)
        assert unicodedata.is_normalized("NFD", rd.fold_name(c)), ascii(c)
    assert rd.fold_name("ΐ") == "ΐ"     # the ruling's example, expanded and decomposed
    assert rd.fold_name("ﬃ") == "ffi"                     # a ligature folds to three letters
    for cp in range(0x10000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        assert unicodedata.is_normalized("NFD", rd.fold_name(chr(cp))), hex(cp)


def test_codex_config_toml_exemption_is_not_a_directory_entry():
    """PRE-L3 fix4 (L1 LOW-3): a ``config.toml/`` entry (a directory marker) is not the rendered file."""
    blob, facts = egress(vendor="codex")
    mine = rd.render(blob, facts).file("config/codex/config.toml").data
    assert [e.name for e in rd.discovery_refusals({"codex": ["config.toml/"]}, codex_config=mine,
                                                  rendered_codex_config=mine)] == ["planted-discovery:codex-config-toml"]
    assert rd.discovery_refusals({"codex": ["config.toml"]}, codex_config=mine, rendered_codex_config=mine) == []


def test_levain_repo_settings_pass():
    """The negative control. ``.claude/settings.json`` is absent from this repo, so the control is (a) a hooks-only
    settings file and (b) the Claude Code settings template Levain ships (hooks + permissions)."""
    assert not (REPO / ".claude" / "settings.json").exists()
    hooks_only = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python3 x.py"}]}]}}
    shipped = json.loads((REPO / "levain/templates/adapters/claude-code/settings.template.json").read_text())
    assert "hooks" in shipped
    assert rd.repo_loosening_refusals({".claude/settings.json": hooks_only}) == []
    assert rd.repo_loosening_refusals({".claude/settings.json": shipped,
                                       ".claude/settings.local.json": {"permissions": {"allow": ["Bash(ls)"]}}}) == []
    rd.refuse_repo_loosening({".claude/settings.json": shipped})


# --- driver_input parse (§5.4) --------------------------------------------------------------------------------

def test_driver_input_parse():
    ok = canon.canonical_bytes({"a": [1, "x"], "b": {"c": None}})
    assert rd.parse_driver_input(ok) == {"a": [1, "x"], "b": {"c": None}}
    deep64 = b"[" * 64 + b"]" * 64
    assert rd.parse_driver_input(deep64) is not None
    for bad in (b"[" * 65 + b"]" * 65, b'{"a":1.5}', b'{"a": 1}', b'{"b":1,"a":2}', b'{"a":1,"a":2}',
                b"\xef\xbb\xbf{}", b"\xff", b'{"a":NaN}', b'{"a":9007199254740993}', b"{"):
        assert refused(rd.parse_driver_input, bad).name == "driver-input-mismatch", bad


def test_wire_name():
    assert rd.wire_name("permissions.disableBypassPermissionsMode") == "permissions-disable-bypass-permissions-mode"
    assert rd.wire_name("AGENTS.override.md") == "agents-override-md"
    assert rd.wire_name("home-.config/git") == "home-config-git"


# --- golden text -----------------------------------------------------------------------------------------------

GOLD_PROFILE_EGRESS = """(version 1)
;; Levain session profile (CONTRACT §8.1) for s-0123456789abcdef on e3, kind interactive.
(allow default)
(deny file-write*)
(allow file-write*
  (subpath "/private/var/levain/live/s-0123456789abcdef/repo") (subpath "/private/var/levain/live/s-0123456789abcdef/config") (subpath "/private/var/levain/live/s-0123456789abcdef/xdg") (subpath "/private/var/levain/live/s-0123456789abcdef/tmp") (subpath "/private/var/levain/live/s-0123456789abcdef/sock")
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper") (literal "/dev/ptmx"))
(deny network-outbound)
(deny network-bind)
(deny network-inbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote unix-socket (subpath "/private/var/levain/live/s-0123456789abcdef/sock")))
(allow network-bind (local unix-socket (subpath "/private/var/levain/live/s-0123456789abcdef/sock")))
(allow network-inbound (local unix-socket (subpath "/private/var/levain/live/s-0123456789abcdef/sock")))
(deny mach-lookup)
(deny mach-register)
(allow mach-lookup (global-name "com.apple.trustd.agent"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
"""

GOLD_PROFILE_DISPATCH_QUEUE_LINES = [
    '(allow network-outbound (remote ip "localhost:47103"))',
    '(allow network-outbound (remote unix-socket (subpath "/private/var/run/levain/qb/slot3")))',
]

GOLD_SETTINGS_LINUX = {"permissions": {"allow": ["WebFetch(domain:api.github.com)", "WebFetch(domain:github.com)"],
                                       "ask": ["Bash(dangerouslyDisableSandbox:true)"],
                                       "disableBypassPermissionsMode": "disable"},
                       "sandbox": {"bwrapPath": "/usr/libexec/levain/bwrap", "enabled": True, "excludedCommands": [],
                                   "failIfUnavailable": True},
                       "disableAllHooks": False, "disableClaudeAiConnectors": True, "skipWebFetchPreflight": True}

GOLD_CODEX_DARWIN = f"""approval_policy = "never"
cli_auth_credentials_store = "file"
model_provider = "levain"
sandbox_mode = "danger-full-access"
web_search = "disabled"

[model_providers.levain]
base_url = "http://127.0.0.1:47103/r/openai/v1"
env_key = "OPENAI_API_KEY"
name = "levain"
wire_api = "responses"

[projects."/private/var/levain/live/{S}/repo"]
trust_level = "untrusted"
"""

GOLD_CODEX_LINUX = f"""approval_policy = "never"
cli_auth_credentials_store = "file"
model_provider = "levain"
web_search = "disabled"

[model_providers.levain]
base_url = "http://127.0.0.1:47100/r/openai/v1"
env_key = "OPENAI_API_KEY"
name = "levain"
wire_api = "responses"

[projects."/var/levain/live/{S}/repo"]
trust_level = "untrusted"
"""


def _digest(r) -> str:
    h = hashlib.sha256()
    for f in sorted(r.files, key=lambda f: f.path):
        h.update(f"{f.path}\0{f.uid}\0{f.gid}\0{f.mode:o}\0".encode() + hashlib.sha256(f.data).digest())
    return h.hexdigest()[:16]


#: Every file (path, owner, mode, bytes) of each (OS x vendor x class) render, pinned. The full text of each was
#: read once when these were taken; a change here is a change to what a confined session loads. WF-1b re-took the four
#: egress digests: the only change against cf2e19bb is root/settings.json's permissions.allow gaining
#: WebFetch(domain:api.github.com) and WebFetch(domain:github.com), the helper's route_hosts for its github route.
#: 1105-10 endvf re-took the four darwin digests: the only change is root/profile.sb losing its var-folders writable
#: group (DESIGN_end_var_folders (e)); the four Linux digests are unchanged.
GOLD_DIGESTS = {
    ("darwin", "claude", "egress"): "882b6a9e58394c4b",
    ("darwin", "claude", "none"): "c15ca48b7909cf0b",
    ("darwin", "codex", "egress"): "c83c5f69a0ceedd3",   # R7: + SSL_CERT_FILE in root/env
    ("darwin", "codex", "none"): "96ccb0d934bbd77a",
    ("linux", "claude", "egress"): "9a6f5684256bdd1c",
    ("linux", "claude", "none"): "c9796d76e53b1d57",
    ("linux", "codex", "egress"): "02b07820fb916f2b",
    ("linux", "codex", "none"): "673582e9c5e81a4e",
}


def test_golden_text_per_os_vendor_class():
    blob, facts = egress()
    assert rd.render(blob, facts).file("root/profile.sb").data.decode() == GOLD_PROFILE_EGRESS
    blob, facts = egress(kind="dispatch", hooks=frozenset({"levain_queue"}),
                         hook_templates={"levain_queue": HookTemplate(b"q", "PreToolUse")})
    lines = rd.render_profile(blob, facts).decode().splitlines()
    for g in GOLD_PROFILE_DISPATCH_QUEUE_LINES:
        assert g in lines
    assert not any("localhost:*" in x for x in lines)
    blob, facts = egress(kind="planner")
    assert '(allow network-outbound (remote unix-socket (subpath "/private/var/run/levain/planner/slot3")))' in \
        rd.render_profile(blob, facts).decode().splitlines()
    blob, facts = verifier()
    v = rd.render_profile(blob, facts).decode()
    assert "remote ip" not in v and "local ip" not in v
    # 1105-10 endvf (DESIGN_end_var_folders (e)): neither uid's var-folders dir is writable, the driver's included
    assert "/private/var/folders" not in v
    assert "/private/var/folders" not in rd.render_profile(*egress()).decode()
    blob, facts = egress("linux")
    assert json.loads(rd.render(blob, facts).file("root/settings.json").data) == GOLD_SETTINGS_LINUX
    blob, facts = egress("darwin", "codex")
    assert rd.codex_toml(blob, facts).decode() == GOLD_CODEX_DARWIN
    blob, facts = egress("linux", "codex")
    assert rd.codex_toml(blob, facts).decode() == GOLD_CODEX_LINUX
    got = {}
    for host, vendor in itertools.product(("darwin", "linux"), ("claude", "codex")):
        got[(host, vendor, "egress")] = _digest(rd.render(*egress(host, vendor)))
        got[(host, vendor, "none")] = _digest(rd.render(*verifier(host, vendor)))
    assert got == GOLD_DIGESTS


def test_egress_route_without_hosts_is_refused_not_rendered_ruleless():
    """WF-1a L3 r1 ruling, closed by WF-1b (wf1a return ruling 2): an egress route the session names with no hosts
    entry was rendered with no allow rule; route_hosts now has its producer (runtime_helper.base_routes), so a missing
    entry is refused by name before any byte."""
    for host in ("darwin", "linux"):
        blob, facts = egress(host, "claude", route_hosts={})
        assert "github" in blob.routes and ROUTES["github"] == "egress"
        assert refused(rd.render, blob, facts).name == "egress-route-no-hosts"


class _Boom:
    def __iter__(self):
        raise RuntimeError("not a host list")


def test_egress_hosts_malformed_facts_refused_by_name():
    """WF-1a L3 r1 W2: a route_hosts fact of the wrong shape is refused ``bad-config``, never a raw traceback."""
    for bad in (None, ["github"], {"github": _Boom()}, {"github": ("github.com", None)}, {"github": "github.com"},
                {"github": {"github.com": 1}}, {"github": ("github.com", b"api.github.com")}):
        blob, facts = egress("darwin", route_hosts=bad)
        e = refused(rd.render, blob, facts)
        assert e.name == "bad-config", bad


def test_egress_hosts_scoped_to_the_egress_class():
    """WF-1a L3 r1 W4: only an egress-class session reads route_hosts; the none class renders none of it."""
    blob, facts = verifier(route_hosts={"github": ("NOT A HOST",)})
    assert rd.egress_hosts(blob, facts) == ()
    rd.render(blob, facts)


def test_codex_files_the_session_repo_untrusted():
    """wf1af return rulings (THE CLASS), from the run (1026-2/wf1ag/RETURN.md, Codex 0.162.1 app-server
    ``config/read``): a ``trusted`` repo's ``.codex/config.toml`` outranked every key this file sets; ``untrusted``
    keeps that layer out. Every session that renders a Codex config files its repo, and only its repo, so."""
    for host, vendor, auth in itertools.product(("darwin", "linux"), ("claude", "codex"), ("brokered", "passthrough")):
        blob, facts = egress(host, vendor, auth=auth)
        tables = rd.codex_config(blob, facts)["tables"]
        projects = {k: v for k, v in tables.items() if k.startswith("projects.")}
        assert projects == {f"projects.{rd._toml_str(str(facts.S / 'repo'))}": {"trust_level": "untrusted"}}
        assert 'trust_level = "trusted"' not in rd.codex_toml(blob, facts).decode()
    blob, facts = verifier(vendor="codex")
    assert 'trust_level = "untrusted"' in rd.codex_toml(blob, facts).decode().splitlines()


def test_codex_launch_pins_web_search_above_a_trusted_repo():
    """WF-1a L3 r1 W1, from the run (1026-2/wf1af/RETURN.md, Codex 0.162.1 app-server ``config/read``): a trusted
    repo's ``.codex/config.toml`` ``web_search = "live"`` wins over CODEX_HOME's ``disabled``; a ``-c`` override
    is the ``sessionFlags`` layer above it, kept now that the repo is filed untrusted. The launch args parse as
    Codex parses ``-c`` (a TOML value)."""
    import tomllib

    args = rd.CODEX_LAUNCH_ARGS
    assert len(args) % 2 == 0 and all(f == "-c" for f in args[::2])
    pinned = {}
    for kv in args[1::2]:
        k, _, v = kv.partition("=")
        pinned[k] = tomllib.loads(f"x = {v}")["x"]
    assert pinned == {"web_search": rd.CODEX_WEB_SEARCH} and rd.CODEX_WEB_SEARCH == "disabled"


def test_repo_settings_proxy_env_of_the_wrong_shape_is_malformed():
    """WF-1a L3 r1 LOW-2: an ``env`` that is not an object is refused as malformed before any name is matched."""
    for env in (["HTTPS_PROXY"], "HTTPS_PROXY=x", None, 1):
        got = rd.repo_loosening_refusals({".claude/settings.json": {"env": env}})
        assert [x.name for x in got] == ["repo-settings-malformed"], env
