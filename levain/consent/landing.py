"""levain.consent.landing — the landing tier of a dispatch result's paths (F11 §4 step 4).

A dispatch land writes files into the operator's repository without any agent's tool call, so no adapter files a
request for them. The landing tier asks what the consent classifiers WOULD file a Write of each path at, had an agent
asked: ``T3`` when either adapter tags the path as agent config or a hook (ruling 3b item 1: Claude Code's
:func:`levain.consent.adapters.claude_code.targets_agent_config`, which also covers git's config and hooks wherever
git says it runs them, the levain package this interpreter imports and the adapter's own queue and authority stores;
and Codex's, which adds anything under a ``.codex/`` directory or CODEX_HOME), else ``T2``. Both adapters'
protected stores count, each read from its own settings in the consent directory. The paths are judged as written
and as resolved through symlinks, the way the adapters judge a Write.

It fails closed by raising: a fault is never an answer here, and the adoption floor refuses a land whose tier it
cannot read. Stdlib only.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = ["landing_tier"]


def landing_tier(repo: str, paths: Iterable[str], *, queue_dir: Path | None = None, home: Path | None = None,
                 codex_dir: Path | None = None) -> str:
    """``"T3"`` when any of ``paths`` (absolute, inside ``repo``) is agent config or a hook to either adapter, else
    ``"T2"``. ``queue_dir`` defaults to the consent directory, ``home`` to the user's, ``codex_dir`` to CODEX_HOME.
    Raises when a classifier cannot run, or when ``paths`` holds anything but absolute paths."""
    from levain.consent.adapters import claude_code, codex
    from levain.consent.queue import default_queue_dir

    qdir = Path(queue_dir) if queue_dir is not None else default_queue_dir()
    home = Path(home) if home is not None else Path.home()
    codex_dir = Path(codex_dir) if codex_dir is not None else codex.codex_home()
    protected = tuple({*claude_code.protected_paths(qdir, claude_code.load_settings(qdir), home=home,
                                                    environ=os.environ, cwd=repo),
                       *claude_code.protected_paths(qdir, codex.load_settings(qdir), home=home,
                                                    environ=os.environ, cwd=repo)})
    git_cache: dict[Any, Any] = {}       # one git budget for the whole change set, as one gate call has
    judged = 0
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"a landing path is absolute, not {str(path)[:80]!r}")
        judged += 1
        if claude_code.targets_agent_config("Write", {"file_path": path}, repo, home=home, environ=os.environ,
                                            protected=protected, git_cache=git_cache):
            return "T3"
        if codex._in_codex_config(path, repo, home, codex_dir):
            return "T3"
    if not judged:
        raise ValueError("a landing tier is read over at least one path")
    return "T2"
