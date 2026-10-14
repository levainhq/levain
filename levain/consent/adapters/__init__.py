"""levain.consent.adapters — the agent hooks that put an external agent's permission gate on the consent queue.

:mod:`levain.consent.adapters.claude_code` is Claude Code's ``PermissionRequest`` hook. An adapter files the gate,
asks the core for a verified decision and reports it, or reports nothing so the agent's own dialog stands. It
never decides: authority is what :class:`levain.consent.ConsentQueue` verified, never what the hook was sent.
"""
