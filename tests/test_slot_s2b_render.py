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
    assert set(env) == BASE_KEYS | PROXY_KEYS
    assert rd.secret_env_names(blob, facts) == ("OPENAI_API_KEY",)
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
            {"type": "command", "command": f"{L}/root/hooks/levain_gate"}]}]}
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
  (subpath "/private/var/folders/zy/hhq908f97y7g9v4yl18g49l00000lq")
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
GOLD_DIGESTS = {
    ("darwin", "claude", "egress"): "18f9a25ffabf303f",
    ("darwin", "claude", "none"): "dd0f5dad5c38e0b8",
    ("darwin", "codex", "egress"): "24cbf6f8faa1bac0",
    ("darwin", "codex", "none"): "7441ce40b3226c61",
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
    assert "remote ip" not in v and "local ip" not in v and '(subpath "/private/var/folders/ab/d2")' in v
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
