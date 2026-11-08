"""The keys of Claude Code's ``sandbox`` settings block that relax the profile, in one place both sides read.

``levain.host_guard`` refuses a managed or user settings layer that sets them, and ``levain.slot.render`` refuses a repo's
settings layer that does (:func:`levain.slot.render.repo_loosening_refusals`). That second reader runs wherever a repo's
snapshot is judged, the dispatch run server included, a service under the host's ``/usr/bin/python3`` (3.9 on macOS), and
``host_guard`` is a large operator-side module, so the lists live here, stdlib-free and importable on 3.9, and both import
them. Each entry is a key path inside ``sandbox``.

``network.allowMachLookup`` naming ``com.apple.trustd.agent`` opens what ``enableWeakerNetworkIsolation`` opens; a run
(levain-memory 1026-8/p1) made trustd fetch a URL a sandboxed command chose, outside the proxy.
"""
from __future__ import annotations

__all__ = ["repo_env_rule", "repo_settings_rule", "LOOSENING_NONEMPTY", "LOOSENING_TRUE"]

#: Keys that must not be ``true``.
LOOSENING_TRUE = (("allowUnsandboxedCommands",), ("enableWeakerNestedSandbox",),
                  ("enableWeakerNetworkIsolation",), ("allowAppleEvents",), ("allowNestedUserNamespaces",),
                  ("network", "allowAllUnixSockets"), ("network", "allowLocalBinding"),
                  ("filesystem", "disabled"))
#: Keys that must be empty lists.
LOOSENING_NONEMPTY = (("excludedCommands",), ("filesystem", "allowWrite"), ("filesystem", "allowRead"),
                      ("network", "allowUnixSockets"), ("network", "allowedDomains"),
                      ("network", "allowMachLookup"))

#: pf4r2 R1: a repo's own settings ``env`` may not name a variable that moves a slot's endpoint, provider, credential,
#: billing identity, tenant or TLS trust. The bound is by name FAMILY, not an exact list, so a vendor variable added
#: later in a covered family is refused by default (an exact list only ever grew, one review round at a time).
#: Names are compared upper-cased. The proxy names keep their own refusal (``levain.slot.render.PROXY_ENV``).
#: A provider's or SDK's own variables, whatever follows the prefix.
REPO_ENV_PREFIXES = ("ANTHROPIC_", "AWS_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_", "CLOUD_ML_", "AZURE_", "OPENAI_",
                     "VERTEX_", "BEDROCK_")
#: An endpoint or a credential, on any stem.
REPO_ENV_SUFFIXES = ("_BASE_URL", "_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_REFRESH_TOKEN", "_SECRET",
                     "_SECRET_KEY", "_CREDENTIALS", "_PROFILE")
#: Claude Code's provider switches (``CLAUDE_CODE_USE_BEDROCK`` and its siblings).
REPO_ENV_SWITCH_PREFIX = "CLAUDE_CODE_USE_"
#: Words that, after ``CLAUDE_CODE_``, name a login or auth step, a TLS client identity or trust store, a transport
#: (socket, proxy, launcher) or a provider: the vendor's reference names e.g. ``CLAUDE_CODE_OAUTH_TOKEN``,
#: ``CLAUDE_CODE_SKIP_VERTEX_AUTH``, ``CLAUDE_CODE_CLIENT_CERT``, ``CLAUDE_CODE_CERT_STORE``,
#: ``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`` and ``CLAUDE_CODE_PROCESS_WRAPPER``.
REPO_ENV_CLAUDE_WORDS = ("AUTH", "CERT", "CLIENT_KEY", "MTLS", "UNIX_SOCKET", "PROVIDER", "PROXY", "WRAPPER",
                         "CRED", "AWS", "BEDROCK", "VERTEX", "FOUNDRY", "MANTLE", "GCP", "AZURE")
#: Single names outside the families: the config dir credentials are read from, the request-body merge, the
#: runtime's TLS trust and options (``NODE_OPTIONS`` can load code into the vendor's own process).
REPO_ENV_NAMES = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_EXTRA_BODY", "NODE_EXTRA_CA_CERTS",
                            "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS", "SSL_CERT_FILE", "SSL_CERT_DIR",
                            "REQUESTS_CA_BUNDLE"})


def repo_env_rule(name: object) -> str | None:
    """The rule that refuses ``name`` in a repo settings ``env``, as text for the refusal's detail (``prefix AWS_``,
    ``suffix _API_KEY``, ``CLAUDE_CODE_ with AUTH``, ``name NODE_OPTIONS``), or None when it may be set."""
    u = str(name).upper()
    if u in REPO_ENV_NAMES:
        return "name " + u
    for p in REPO_ENV_PREFIXES:
        if u.startswith(p):
            return "prefix " + p
    for s in REPO_ENV_SUFFIXES:
        if u.endswith(s):
            return "suffix " + s
    if u.startswith(REPO_ENV_SWITCH_PREFIX):
        return "prefix " + REPO_ENV_SWITCH_PREFIX
    if u.startswith("CLAUDE_CODE_"):
        for w in REPO_ENV_CLAUDE_WORDS:
            if w in u[len("CLAUDE_CODE_"):]:
                return "CLAUDE_CODE_ with " + w
    return None


#: pf4r2 R2: the endings (compared case-insensitively) of a Claude Code settings key that runs a command to supply a
#: credential or request headers; the vendor's settings reference documents ``apiKeyHelper``, ``otelHeadersHelper``,
#: ``awsAuthRefresh``, ``gcpAuthRefresh`` and ``awsCredentialExport`` so, each with scope "Any file". A repo's
#: settings layer naming such a key is refused, whatever its value; a later provider's sibling is refused by default.
REPO_SETTINGS_SUFFIXES = ("helper", "authrefresh", "credentialexport")


def repo_settings_rule(key: object) -> str | None:
    """The rule that refuses top-level settings key ``key`` in a repo layer, as text for the refusal's detail
    (``suffix helper``), or None when it may be set."""
    k = str(key).casefold()
    for s in REPO_SETTINGS_SUFFIXES:
        if k.endswith(s):
            return "suffix " + s
    return None
