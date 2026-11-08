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

__all__ = ["CLAUDE_CREDENTIAL_ENV", "REPO_CREDENTIAL_SETTINGS", "LOOSENING_NONEMPTY", "LOOSENING_TRUE"]

#: Keys that must not be ``true``.
LOOSENING_TRUE = (("allowUnsandboxedCommands",), ("enableWeakerNestedSandbox",),
                  ("enableWeakerNetworkIsolation",), ("allowAppleEvents",), ("allowNestedUserNamespaces",),
                  ("network", "allowAllUnixSockets"), ("network", "allowLocalBinding"),
                  ("filesystem", "disabled"))
#: Keys that must be empty lists.
LOOSENING_NONEMPTY = (("excludedCommands",), ("filesystem", "allowWrite"), ("filesystem", "allowRead"),
                      ("network", "allowUnixSockets"), ("network", "allowedDomains"),
                      ("network", "allowMachLookup"))

#: Every Claude Code env var that moves a slot's credential, billing identity or endpoint, in one place; a repo's
#: own settings ``env`` naming any of them is refused.
#: Each name is one the vendor's env-var reference carries (``https://code.claude.com/docs/en/env-vars``): a key
#: or bearer (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_FOUNDRY_API_KEY``,
#: ``AWS_BEARER_TOKEN_BEDROCK``), a subscription login (``CLAUDE_CODE_OAUTH_TOKEN`` and its refresh token), a provider
#: switch (``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` / ``_FOUNDRY``), extra request headers
#: (``ANTHROPIC_CUSTOM_HEADERS``), the endpoint (``ANTHROPIC_BASE_URL``) and the config dir credentials are read from
#: (``CLAUDE_CONFIG_DIR``).
CLAUDE_CREDENTIAL_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY", "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR",
})
#: Claude Code settings keys that supply a credential by running a command (the vendor's settings reference lists
#: each with scope "Any file"); a repo's settings layer naming one is refused, whatever its value.
REPO_CREDENTIAL_SETTINGS = ("apiKeyHelper", "awsCredentialExport", "awsAuthRefresh")
