"""Sudoers text for the two helpers (CONTRACT §5.1, §5.2).

Runtime helper: two NOPASSWD rules, the operator's (every verb) and ``_levain_dispatch``'s (the verbs
§5.1 grants it; the ticket binding is the helper's own check, which sudo cannot express), under
``use_pty``. Install helper: the operator's rule WITH a password on every call
(``timestamp_timeout=0``). The operator name passes :func:`levain.slot.grammar.check_operator_name`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from levain.firing.hands import _SUDOERS_DIR, HostOS, Step, _abs
from levain.slot.grammar import check_operator_name
from levain.slot.layout import install_helper_path, runtime_helper_path

if TYPE_CHECKING:
    from levain.slot.removal import Live

#: sudo skips a sudoers.d file whose name contains a "." or ends in "~"; these names have neither.
RUNTIME_SUDOERS = _SUDOERS_DIR / "levain-runtime"
INSTALL_SUDOERS = _SUDOERS_DIR / "levain-install"

#: The first line of every drop-in this module writes, both rules, every version since S1 (``git log -S`` on it in
#: this file): :func:`dropin_state` reads a recorded drop-in as Levain's only when its first line starts with it.
HEADER = "# Written by the Levain install helper"

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
        f"{HEADER} (CONTRACT §5.1); removed by its undo verb.\n"
        f"Defaults!{h} use_pty\n"
        f"{op} ALL=(root) NOPASSWD: {h}\n"
        f"_levain_dispatch ALL=(root) NOPASSWD: {dispatch_cmds}\n"
    )


def install_sudoers_text(host: HostOS, operator: str) -> str:
    op = check_operator_name(operator)
    h = str(install_helper_path(host))
    return (
        f"{HEADER} (CONTRACT §5.2, §23); removed by its undo verb.\n"
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


def sudoers_paths() -> tuple[Path, Path]:
    return RUNTIME_SUDOERS, INSTALL_SUDOERS


def _is_ours(data: bytes) -> bool:
    return data.split(b"\n", 1)[0].startswith(HEADER.encode())


def dropin_state(name: str, live: "Live") -> str:
    """``sudoers/1``'s live outcome: OURS only for a regular root file whose first line starts with :data:`HEADER`. Any
    other file at the recorded name (an admin's own rule there, say) is NOT_OURS: never removed (L3 r1 codex HIGH)."""
    from levain.slot.removal import root_file

    return root_file(live, _SUDOERS_DIR / name, check=_is_ours)


def config_check_step(host: HostOS, why: str, *, live: "Live", refusal: str, remedy: bool = False) -> Step:
    """``visudo -c`` of the whole sudoers config as its own step. ``refusal`` leads the failure text: what the run has
    and has not done when the config does not parse. ``remedy``: the failure text also names each drop-in Levain
    wrote (:func:`dropin_state` OURS) that visudo flagged, with the command that removes it (L3 r2 complement LOW 2:
    undo's preflight cannot get past its own broken drop-in, so the operator is given the command)."""
    from levain.slot.removal import RemoveStep

    check = (_visudo(host)[0], "-c")

    def go() -> tuple[bool, str]:
        rc, out, err = live.out(check)
        if rc == 0:
            return True, ""
        text = f"{refusal}: {' '.join(check)} exited {rc}: {(err or out).strip()[:200]}"
        fixes = [f"{_SUDOERS_DIR / n} (remove it: sudo {_rm(host)} -f {_SUDOERS_DIR / n})"
                 for n in (_flagged_dropins(f"{out}\n{err}") if remedy else ()) if _written_by_us(n, live)]
        return False, text + (f"; Levain's own drop-ins visudo flagged: {', '.join(fixes)}" if fixes else "")
    return RemoveStep(why, call=go, tools=(check[0],))


def _written_by_us(name: str, live: "Live") -> bool:
    from levain.slot.removal import OURS, ReadFailed

    try:
        return dropin_state(name, live) == OURS
    except ReadFailed:            # unreadable: not named, the visudo text above still is
        return False


def _flagged_dropins(text: str) -> list[str]:
    """The drop-in names ``visudo -c`` output names on a line that is not its ``parsed OK`` report, in order."""
    pat = re.compile(re.escape(str(_SUDOERS_DIR)) + r"/([^\s:<>,]+)")
    names: list[str] = []
    for line in text.splitlines():
        if "parsed OK" not in line:
            names += [n for n in pat.findall(line) if _NAME.fullmatch(n) and n not in names]
    return names


#: A drop-in name sudo reads (no "." and no trailing "~", see :data:`RUNTIME_SUDOERS`).
_NAME = re.compile(r"[A-Za-z0-9_-]+")


def remove_dropin_steps(host: HostOS, name: str, *, live: "Live") -> list[Step]:
    """Row 33 R1b, ``sudoers/1`` (§1.1 table row 2, R5): the recorded drop-in ``_SUDOERS_DIR/<name>`` removed when it
    is a regular root file, then ``visudo -c`` of the whole config as its OWN step, run whatever the drop-in's
    outcome: a re-run after a failed check finds the drop-in ABSENT and must still check, never pass silently. Never
    a current name looked up."""
    from levain.slot.removal import at, chain, point, remove_step

    path = _SUDOERS_DIR / name
    rm = _rm(host)
    return [remove_step(f"remove {path} (visudo -c checks the config next)", ("sudoers/1", name), live,
                        lambda: dropin_state(name, live),
                        chain(lambda: live.act((rm, "-f", at(live, path))), point(live, "sudoers/1:after-rm")),
                        tools=(rm,)),
            config_check_step(host, f"check the sudoers config with visudo -c (after {path})", live=live,
                              refusal=f"the sudoers config does not parse after {path} was removed")]
