"""A stub ``claude`` for the P10a rig: the program the dream's fire runs when the install fact ``claude_bin`` names it.

The fire builds its mind as ``ClaudeMind(inst.model, claude_bin=inst.claude_bin)`` and starts it as a real child
process, so a file at that path is the whole seam. This file is that program (a POSIX ``sh`` script) and the test-side
handle on what it did. The script has its directory written into it, because the fire's child carries a scrubbed
environment and could not read one from a variable.

Per call n (1-based) the script records ``argv-n`` (one argument per line), ``stdin-n`` and ``cwd-n`` beside itself,
then replies from ``result-n.json`` (else ``result.json``, else a minimal success document), sleeps for ``sleep-n`` /
``sleep`` seconds first if that file exists, and exits with ``exit-n`` / ``exit`` if that file exists.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

_SCRIPT = """#!/bin/sh
D='{dir}'
n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/n"
printf '%s\\n' "$@" > "$D/argv-$n"
pwd > "$D/cwd-$n"
cat > "$D/stdin-$n"
for k in "sleep-$n" sleep; do [ -f "$D/$k" ] && {{ sleep "$(cat "$D/$k")"; break; }}; done
if [ -f "$D/result-$n.json" ]; then cat "$D/result-$n.json"
elif [ -f "$D/result.json" ]; then cat "$D/result.json"
else cat "$D/default.json"; fi
for k in "exit-$n" exit; do [ -f "$D/$k" ] && exit "$(cat "$D/$k")"; done
exit 0
"""


def success_doc(text: str = "stub", cost: float | None = 0.01, model: str = "claude-opus-5-5") -> dict:
    doc = {"type": "result", "subtype": "success", "is_error": False, "result": text,
           "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0},
           "modelUsage": {model: {"costUSD": cost}}}
    if cost is not None:
        doc["total_cost_usd"] = cost
    return doc


class StubClaude:
    """``StubClaude(root)`` writes ``root/claude`` (executable) and ``root/calls/``. ``path`` is the value for ``claude_bin``."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "calls"
        self.dir.mkdir(parents=True)
        self.path = Path(root) / "claude"
        self.path.write_text(_SCRIPT.format(dir=self.dir), encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        (self.dir / "default.json").write_text(json.dumps(success_doc()), encoding="utf-8")

    def reply(self, doc: dict | str, n: int | None = None) -> None:
        """What call ``n`` (every call, if None) prints on stdout."""
        body = doc if isinstance(doc, str) else json.dumps(doc)
        (self.dir / ("result.json" if n is None else f"result-{n}.json")).write_text(body, encoding="utf-8")

    def sleep(self, seconds: float, n: int | None = None) -> None:
        (self.dir / ("sleep" if n is None else f"sleep-{n}")).write_text(str(seconds), encoding="utf-8")

    def exit_code(self, rc: int, n: int | None = None) -> None:
        (self.dir / ("exit" if n is None else f"exit-{n}")).write_text(str(rc), encoding="utf-8")

    def calls(self) -> int:
        f = self.dir / "n"
        return int(f.read_text()) if f.exists() else 0

    def argv(self, n: int = 1) -> list[str]:
        return (self.dir / f"argv-{n}").read_text(encoding="utf-8").splitlines()

    def stdin(self, n: int = 1) -> str:
        return (self.dir / f"stdin-{n}").read_text(encoding="utf-8")
