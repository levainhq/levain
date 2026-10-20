"""Sudoers text for the two helpers (CONTRACT §5.1, §5.2).

Runtime helper: two NOPASSWD rules, the operator's (every verb) and ``_levain_dispatch``'s (the verbs
§5.1 grants it; the ticket binding is the helper's own check, which sudo cannot express), under
``use_pty``. Install helper: the operator's rule WITH a password on every call
(``timestamp_timeout=0``). The operator name passes :func:`levain.slot.grammar.check_operator_name`.
"""
from __future__ import annotations

from pathlib import Path

from levain.firing.hands import _SUDOERS_DIR, HostOS, Step, _abs
from levain.slot.grammar import check_operator_name
from levain.slot.layout import install_helper_path, runtime_helper_path

#: sudo skips a sudoers.d file whose name contains a "." or ends in "~"; these names have neither.
RUNTIME_SUDOERS = _SUDOERS_DIR / "levain-runtime"
INSTALL_SUDOERS = _SUDOERS_DIR / "levain-install"

#: CONTRACT §5.1: the runtime verbs ``_levain_dispatch`` may call (every verb but adopt-open,
#: discard, restore, confirm-identity).
DISPATCH_VERBS = ("prepare", "render", "start", "resume", "teardown", "status")


def runtime_sudoers_text(host: HostOS, operator: str) -> str:
    op = check_operator_name(operator)
    h = str(runtime_helper_path(host))
    # sudo's `cmd *` needs at least one argument (measured in the 1c VM); the bare form lists `status`
    # with no arguments.
    dispatch_cmds = ", ".join([*(f"{h} {v} *" for v in DISPATCH_VERBS), f"{h} status"])
    return (
        "# Written by the Levain install helper (CONTRACT §5.1); removed by its undo verb.\n"
        f"Defaults!{h} use_pty\n"
        f"{op} ALL=(root) NOPASSWD: {h}\n"
        f"_levain_dispatch ALL=(root) NOPASSWD: {dispatch_cmds}\n"
    )


def install_sudoers_text(host: HostOS, operator: str) -> str:
    op = check_operator_name(operator)
    h = str(install_helper_path(host))
    return (
        "# Written by the Levain install helper (CONTRACT §5.2, §23); removed by its undo verb.\n"
        # §23: the password is asked unless a LATER sudoers rule for this path says NOPASSWD (root-written,
        # so trusted): with several matching rules sudo applies the last one.
        f"Defaults!{h} authenticate, timestamp_timeout=0\n"
        f"{op} ALL=(root) PASSWD: {h}\n"
    )


def _visudo(host: HostOS) -> tuple[str, ...]:
    return ("/usr/sbin/visudo", "-cf") if host == "darwin" else (_abs("visudo"), "-cf")


def sudoers_steps(host: HostOS, operator: str) -> list[Step]:
    return [
        Step("let the runtime helper run without a password (sudoers drop-in, checked with visudo)",
             write=(RUNTIME_SUDOERS, runtime_sudoers_text(host, operator), 0o440), validate=_visudo(host)),
        Step("let the install helper run, asking a password every call (sudoers drop-in)",
             write=(INSTALL_SUDOERS, install_sudoers_text(host, operator), 0o440), validate=_visudo(host)),
    ]


def _rm(host: HostOS) -> str:
    return "/bin/rm" if host == "darwin" else _abs("rm")


def remove_runtime_sudoers_steps(host: HostOS) -> list[Step]:
    """Undo's FIRST step (CONTRACT §23): no new runtime-helper calls."""
    return [Step(f"remove {RUNTIME_SUDOERS} (no new runtime-helper calls)", (_rm(host), "-f", str(RUNTIME_SUDOERS)))]


def remove_install_sudoers_steps(host: HostOS) -> list[Step]:
    """Undo's last removal before the operator record (CONTRACT §23): until it goes, a failed undo
    re-runs through the install helper."""
    return [Step(f"remove {INSTALL_SUDOERS} (the install helper's rule, last)", (_rm(host), "-f", str(INSTALL_SUDOERS)))]


def sudoers_paths() -> tuple[Path, Path]:
    return RUNTIME_SUDOERS, INSTALL_SUDOERS
