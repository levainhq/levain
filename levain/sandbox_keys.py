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

from levain.claude_code_env_names import VENDOR_ENV_NAMES

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

#: pf4r3: a repo's own settings ``env`` may not set a variable that moves a slot's endpoint, provider, credential,
#: identity, tenant, TLS trust, telemetry destination, code loading or sandbox profile. Inside the vendor's namespace
#: the bound is an ALLOWLIST (default deny): a vendor variable added later is refused until judged, and a harmless one
#: (a model choice, a cap, a timeout, an off switch) is allowed by name. Outside it, a project's own names launch
#: unless a provider prefix or a credential or endpoint suffix marks them. Names are compared upper-cased.
#: The vendor's namespace, beyond the names its reference documents: any name with one of these prefixes ...
REPO_ENV_VENDOR_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "BETA_", "OTEL_", "DISABLE_", "MCP_")
#: ... and the runtime's TLS trust and options (``NODE_OPTIONS`` can load code into the vendor's own process).
REPO_ENV_VENDOR_NAMES = frozenset({"NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
                                   "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"})
#: The vendor names a repo may set: each one's reference row changes none of the above; the model it runs, an output
#: or token cap, a timeout or retry, a cache TTL, a telemetry or traffic OFF switch, a tool or command turned off, a
#: display toggle, or a documented no-op. Not on it, though harmless-looking: a name Claude Code sets for its own
#: subprocesses (the session's identity to a hook), a name levain renders into the slot's env (a repo's value would
#: replace levain's), a hook-timing or hook-off mode, a removed safety prompt, the model a permission classifier
#: runs on, and prompt text a repo would author for Claude to receive.
REPO_ENV_ALLOW = frozenset({
    "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES", "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "API_FORCE_IDLE_TIMEOUT", "API_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_OUTPUT_LENGTH",
    "BASH_MAX_TIMEOUT_MS", "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS", "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_AUTO_BACKGROUND_TASKS", "CLAUDE_AX_PREPARK_MS",
    "CLAUDE_AX_SCREEN_READER", "CLAUDE_AX_STARTUP_QUIET_MS", "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
    "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS", "CLAUDE_CODE_ACCESSIBILITY", "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
    "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_COMMENTS",
    "CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT", "CLAUDE_CODE_ARTIFACT_PUBLISH_EDITS", "CLAUDE_CODE_ATTRIBUTION_HEADER",
    "CLAUDE_CODE_AUTO_BACKGROUND_WORKER_CHECKIN_SECONDS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_BASH_EDIT_DIFF", "CLAUDE_CODE_BS_AS_CTRL_BACKSPACE",
    "CLAUDE_CODE_CONFIG_LOCK_WAIT_MS", "CLAUDE_CODE_CONNECT_TIMEOUT_MS", "CLAUDE_CODE_DEBUG_LOG_LEVEL",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
    "CLAUDE_CODE_DISABLE_AGENT_VIEW", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "CLAUDE_CODE_DISABLE_ARTIFACT",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS", "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF", "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS",
    "CLAUDE_CODE_DISABLE_CFC_PROMPT", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS",
    "CLAUDE_CODE_DISABLE_FAST_MODE", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING",
    "CLAUDE_CODE_DISABLE_FILE_MENTIONS", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS",
    "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP", "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND",
    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK", "CLAUDE_CODE_DISABLE_MOUSE", "CLAUDE_CODE_DISABLE_MOUSE_CLICKS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
    "CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
    "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK", "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_DISABLE_THINKING",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL",
    "CLAUDE_CODE_DISABLE_WEB_FETCH", "CLAUDE_CODE_DISABLE_WORKFLOWS", "CLAUDE_CODE_DROP_UNAVAILABLE_DEFERRED_TOOL",
    "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_ENABLE_AUTO_MODE", "CLAUDE_CODE_ENABLE_AWAY_SUMMARY",
    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING", "CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE",
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION", "CLAUDE_CODE_ENABLE_SHARE_BUNDLE", "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_ENABLE_TODO_TOOLS", "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_FORCE_STRIKETHROUGH", "CLAUDE_CODE_FORCE_SYNC_OUTPUT", "CLAUDE_CODE_FORCE_TERMINAL_IMAGES",
    "CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS", "CLAUDE_CODE_GLOB_HIDDEN",
    "CLAUDE_CODE_GLOB_NO_IGNORE", "CLAUDE_CODE_GLOB_TIMEOUT_SECONDS", "CLAUDE_CODE_GOAL_CHECKIN_MINUTES",
    "CLAUDE_CODE_GZIP_REQUEST_BODIES", "CLAUDE_CODE_HIDE_CWD", "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL",
    "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_RETRIES",
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", "CLAUDE_CODE_MAX_TURNS",
    "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION", "CLAUDE_CODE_MCP_ALLOWLIST_ENV", "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS",
    "CLAUDE_CODE_MCP_STARTUP_WAIT_MS", "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "CLAUDE_CODE_NATIVE_CURSOR",
    "CLAUDE_CODE_NEW_INIT", "CLAUDE_CODE_NONBLOCKING_STDOUT", "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES",
    "CLAUDE_CODE_NO_FLICKER", "CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE", "CLAUDE_CODE_OTEL_DIAG_STDERR",
    "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS", "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS",
    "CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS", "CLAUDE_CODE_OVERLOADED_RETRY_MAX_DELAY_MS",
    "CLAUDE_CODE_PERFORCE_MODE", "CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS", "CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY",
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "CLAUDE_CODE_PROMPT_CACHE_TTL", "CLAUDE_CODE_RESTRICTED",
    "CLAUDE_CODE_RESUME_INTERRUPTED_TURN", "CLAUDE_CODE_RESUME_INTERRUPTED_TURN_MAX_AGE_MS",
    "CLAUDE_CODE_RESUME_SHOWS_CUT_OFF_CALLS", "CLAUDE_CODE_RETRY_WATCHDOG",
    "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS", "CLAUDE_CODE_RETRY_WATCHDOG_USAGE_LIMIT_RECHECK_MS",
    "CLAUDE_CODE_SCRIPT_CAPS", "CLAUDE_CODE_SCROLL_SPEED", "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT",
    "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS", "CLAUDE_CODE_SKIP_PROMPT_HISTORY", "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "CLAUDE_CODE_SYNTAX_HIGHLIGHT",
    "CLAUDE_CODE_TMUX_TRUECOLOR", "CLAUDE_CODE_TRANSCRIPT_LOCAL_GC", "CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS",
    "CLAUDE_CODE_WARN_DISK_FREE_GIB", "CLAUDE_CODE_WARN_MEMORY_FREE_GIB", "CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS",
    "CLAUDE_CODE_WEBFETCH_DEADLINE_MS", "CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR",
    "CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE", "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS",
    "CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS", "CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG", "CLAUDE_ENABLE_BYTE_WATCHDOG",
    "CLAUDE_ENABLE_STREAM_WATCHDOG", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS", "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
    "CLAUDE_SUBAGENT_BG_SHELL_MAX_MS", "DEBUG", "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "DISABLE_COST_WARNINGS",
    "DISABLE_DOCTOR_COMMAND", "DISABLE_ERROR_REPORTING", "DISABLE_EXTRA_USAGE_COMMAND", "DISABLE_FEEDBACK_COMMAND",
    "DISABLE_GROWTHBOOK", "DISABLE_INSTALLATION_CHECKS", "DISABLE_INSTALL_GITHUB_APP_COMMAND",
    "DISABLE_INTERLEAVED_THINKING", "DISABLE_LOGIN_COMMAND", "DISABLE_LOGOUT_COMMAND", "DISABLE_PROMPT_CACHING",
    "DISABLE_PROMPT_CACHING_FABLE", "DISABLE_PROMPT_CACHING_HAIKU", "DISABLE_PROMPT_CACHING_OPUS",
    "DISABLE_PROMPT_CACHING_SONNET", "DISABLE_TELEMETRY", "DISABLE_UPDATES", "DISABLE_UPGRADE_COMMAND", "DO_NOT_TRACK",
    "ENABLE_PROMPT_CACHING_1H", "ENABLE_TOOL_SEARCH", "FORCE_HYPERLINK", "FORCE_PROMPT_CACHING_5M", "IS_DEMO",
    "MAX_MCP_OUTPUT_TOKENS", "MAX_STRUCTURED_OUTPUT_RETRIES", "MAX_THINKING_TOKENS", "MCP_CONNECTION_NONBLOCKING",
    "MCP_CONNECT_TIMEOUT_MS", "MCP_DISCOVERY_CACHE", "MCP_DISCOVERY_CACHE_MAX_STALE_S", "MCP_DISCOVERY_CACHE_STRIKES",
    "MCP_DISCOVERY_CACHE_TTL_S", "MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE", "MCP_SERVER_CONNECTION_BATCH_SIZE",
    "MCP_TIMEOUT", "MCP_TOOL_TIMEOUT", "SLASH_COMMAND_TOOL_CHAR_BUDGET", "TASK_MAX_OUTPUT_LENGTH"
})
#: Outside the namespace: a provider's or SDK's own variables, whatever follows the prefix.
REPO_ENV_PREFIXES = ("AWS_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_", "CLOUD_ML_", "AZURE_", "OPENAI_", "VERTEX_",
                     "BEDROCK_")
#: Outside the namespace: an endpoint, a transport or a credential, on any stem.
REPO_ENV_SUFFIXES = ("_BASE_URL", "_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_REFRESH_TOKEN", "_SECRET",
                     "_SECRET_KEY", "_CREDENTIALS", "_PROFILE", "_ENDPOINT", "_TOKEN", "_SOCKET", "_URL")


def repo_env_rule(name: object) -> str | None:
    """The rule that refuses ``name`` in a repo settings ``env``, as text for the refusal's detail (``not on the repo
    env allowlist``, ``prefix AWS_``, ``suffix _API_KEY``), or None when it may be set."""
    u = str(name).upper()
    if u in VENDOR_ENV_NAMES or u in REPO_ENV_VENDOR_NAMES or u.startswith(REPO_ENV_VENDOR_PREFIXES):
        return None if u in REPO_ENV_ALLOW else "not on the repo env allowlist"
    for p in REPO_ENV_PREFIXES:
        if u.startswith(p):
            return "prefix " + p
    for s in REPO_ENV_SUFFIXES:
        if u.endswith(s):
            return "suffix " + s
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
