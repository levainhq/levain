"""Run one levain hook script from an agent's MACHINE-WIDE hooks file, so a gone install never blocks the agent.

Codex reads one ``hooks.json`` per CODEX_HOME, so a levain install's Codex hooks run in every Codex session on
the machine, whichever directory it starts in. Measured on codex-cli 0.162.1: a hook that EXITS 2 blocks (UserPromptSubmit: the prompt never
reaches the model; PermissionRequest: "Blocked by hook"), while exit 1 or 127 shows "Hook failed" and carries
on. ``python <script>`` exits 2 when the script file is missing, so ``python <install>/activation/hooks/x.py``
turned a deleted or moved install into a block on every Codex prompt.

The rendered command is therefore ``<python> -EP -m levain.hookrun <install>/activation/hooks/<hook>.py``:
python never opens the script itself. This entry exits 0 printing nothing when the script is not a file,
runs it as ``__main__`` otherwise, and maps every failure (an exception, a non-zero SystemExit, argparse's 2)
to exit 1. A deliberate decision is the JSON the script prints on stdout, never an exit code. A missing
interpreter is the shell's 127 and a venv without levain is python's 1: both fall through as well.

The script path stays the command's last token, the one every reader of the command shape takes as the hook
(the Codex owner matcher, the hook pins, doctor's wiring rows, verify-hooks).
"""

from __future__ import annotations

import os
import runpy
import sys


def main(argv: list[str]) -> int:
    if not argv:
        return 1
    script = argv[0]
    if not os.path.isfile(script):
        return 0
    sys.argv = [script, *argv[1:]]
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return 0 if exc.code in (None, 0) else 1
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
