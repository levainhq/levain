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
    dispatch_cmds = ", ".join(f"{h} {v} *" for v in DISPATCH_VERBS) + f", {h} status"
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
        "# Written by the Levain install helper (CONTRACT §5.2); removed by its undo verb.\n"
        f"Defaults!{h} timestamp_timeout=0\n"
        f"{op} ALL=(root) {h}\n"
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


def remove_sudoers_steps(host: HostOS) -> list[Step]:
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    return [Step(f"remove {p} (no new helper calls)", (rm, "-f", str(p))) for p in (RUNTIME_SUDOERS, INSTALL_SUDOERS)]


def sudoers_paths() -> tuple[Path, Path]:
    return RUNTIME_SUDOERS, INSTALL_SUDOERS
