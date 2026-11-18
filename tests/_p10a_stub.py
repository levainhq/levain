"""A stub ``claude`` for the P10a rig: the program the dream's fire runs when the install fact ``claude_bin`` names it.

The fire builds its mind as ``ClaudeMind(inst.model, claude_bin=inst.claude_bin)`` and starts it as a real child
process, so a file at that path is the whole seam. This file builds that program (a tiny native launcher that runs a
POSIX ``sh`` script) and is the test-side handle on what it did. The script has its directory written into it, because
the fire's child carries a scrubbed environment and could not read one from a variable.

Per call n (1-based) the script records ``argv-n`` (one argument per line), ``stdin-n`` and ``cwd-n`` beside itself,
then replies from ``result-n.json`` (else ``result.json``, else a minimal success document), sleeps for ``sleep-n`` /
``sleep`` seconds first if that file exists, then waits for as long as ``hold-n`` exists (a gate the test removes with
``release(n)``, so a test waits on what it observes, never a wall-clock guess), and exits with ``exit-n`` / ``exit`` if that file exists.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

_SCRIPT = """#!/bin/sh
D='{dir}'
n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/n.tmp.$$" && mv "$D/n.tmp.$$" "$D/n"
printf '%s\\n' "$@" > "$D/argv-$n"
pwd > "$D/cwd-$n"
cat > "$D/stdin-$n"
for k in "sleep-$n" sleep; do [ -f "$D/$k" ] && {{ sleep "$(cat "$D/$k")"; break; }}; done
while [ -f "$D/hold-$n" ]; do sleep 0.1; done
if [ -f "$D/result-$n.json" ]; then cat "$D/result-$n.json"
elif [ -f "$D/result.json" ]; then cat "$D/result.json"
else cat "$D/default.json"; fi
for k in "exit-$n" exit; do [ -f "$D/$k" ] && exit "$(cat "$D/$k")"; done
exit 0
"""


def group_alive(pgid: int) -> bool:
    """Is process group ``pgid`` still there? macOS answers EPERM while the group holds only unreaped zombies: ended."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# A native program has no interpreter in its command line, which is what ``ps`` reads for the real ``claude`` (a native
# executable on the one install measured: claude 2.1.327, 11-18, levain#131). So the stub's entry point is this
# launcher: it is started with the argv the fire records, and runs the ``sh`` script as its child. SIGTERM and SIGINT
# are blocked until the child exists, so one that lands in that window is forwarded to it, never dropped.
_LAUNCHER = r"""#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t kid;
static void fwd(int sig) { if (kid > 0) kill(kid, sig); }

int main(int argc, char **argv) {
    char **a = calloc((argc > 0 ? (size_t)argc : 1) + 2, sizeof *a);
    int st, i;
    sigset_t block, old;
    a[0] = "/bin/sh";
    a[1] = {script};
    for (i = 1; i < argc; i++) a[i + 1] = argv[i];
    sigemptyset(&block);
    sigaddset(&block, SIGTERM);
    sigaddset(&block, SIGINT);
    sigprocmask(SIG_BLOCK, &block, &old);
    signal(SIGTERM, fwd);
    signal(SIGINT, fwd);
    kid = fork();
    if (kid < 0) return 127;
    if (kid == 0) {
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        sigprocmask(SIG_SETMASK, &old, NULL);
        execv("/bin/sh", a);
        _exit(127);
    }
    sigprocmask(SIG_SETMASK, &old, NULL);
    while (waitpid(kid, &st, 0) < 0) if (errno != EINTR) return 127;
    kid = 0;                       /* reaped: its number may be anyone's now, so fwd must not signal it */
    if (WIFSIGNALED(st)) {         /* die the way the child died, as a signalled native program does */
        signal(WTERMSIG(st), SIG_DFL);
        raise(WTERMSIG(st));
    }
    return WEXITSTATUS(st);
}
"""


def _c_string(text: str) -> str:
    """``text`` as a C string literal: printable ASCII as is, every other byte (and the quote, the backslash and the question mark, which can open a trigraph) as an octal escape."""
    out = []
    for b in text.encode("utf-8"):
        out.append(chr(b) if 32 <= b < 127 and chr(b) not in '"\\?' else "\\%03o" % b)
    return '"' + "".join(out) + '"'


def _build_launcher(src: Path, out: Path, script: Path) -> bool:
    """Compile the launcher for ``script`` to ``out``. False only when no C compiler is on the machine (the caller falls back
    to the script itself and the cases that need the native shape skip); a compiler that is there and fails is an error
    carrying its output, never a silent skip, and so is a missing compiler where ``CI`` is set."""
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        if os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no"):
            raise RuntimeError("no C compiler on a CI runner: the p10a stub's native launcher cannot be built")
        return False
    src.write_text(_LAUNCHER.replace("{script}", _c_string(str(script))), encoding="utf-8")
    done = subprocess.run([cc, "-o", str(out), str(src)], capture_output=True, timeout=120)
    if done.returncode != 0 or not out.is_file():
        raise RuntimeError(f"the p10a stub launcher failed to compile with {cc}: {done.stderr.decode(errors='replace')[-600:]}")
    return True


def success_doc(text: str = "stub", cost: float | None = 0.01, model: str = "claude-opus-5-5") -> dict:
    doc = {"type": "result", "subtype": "success", "is_error": False, "result": text,
           "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0},
           "modelUsage": {model: {"costUSD": cost}}}
    if cost is not None:
        doc["total_cost_usd"] = cost
    return doc


class StubClaude:
    """``StubClaude(root)`` writes ``root/claude`` (executable; the launcher, with ``claude.sh`` and ``launcher.c`` beside it) and ``root/calls/``. ``path`` is the value for ``claude_bin``.

    ``native`` is True when ``path`` is a compiled launcher (the shape the real ``claude`` has to ``ps``: its command line
    is the recorded argv). With no C compiler ``path`` is the ``sh`` script itself and ``ps`` shows ``/bin/sh <path> ...``
    (``native`` False): a case that needs the real shape skips on it."""

    def __init__(self, root: Path) -> None:
        root = Path(root)
        self.dir = root / "calls"
        self.dir.mkdir(parents=True)
        self.path = root / "claude"
        script = root / "claude.sh"
        script.write_text(_SCRIPT.format(dir=self.dir), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.native = _build_launcher(root / "launcher.c", self.path, script)
        if not self.native:
            shutil.copy(script, self.path)
        (self.dir / "default.json").write_text(json.dumps(success_doc()), encoding="utf-8")

    def reply(self, doc: dict | str, n: int | None = None) -> None:
        """What call ``n`` (every call, if None) prints on stdout."""
        body = doc if isinstance(doc, str) else json.dumps(doc)
        (self.dir / ("result.json" if n is None else f"result-{n}.json")).write_text(body, encoding="utf-8")

    def sleep(self, seconds: float, n: int | None = None) -> None:
        (self.dir / ("sleep" if n is None else f"sleep-{n}")).write_text(str(seconds), encoding="utf-8")

    def hold(self, n: int = 1) -> None:
        """Call ``n`` will not reply until ``release(n)``."""
        (self.dir / f"hold-{n}").write_text("", encoding="utf-8")

    def release(self, n: int = 1) -> None:
        (self.dir / f"hold-{n}").unlink(missing_ok=True)

    def exit_code(self, rc: int, n: int | None = None) -> None:
        (self.dir / ("exit" if n is None else f"exit-{n}")).write_text(str(rc), encoding="utf-8")

    def calls(self) -> int:
        try:
            return int((self.dir / "n").read_text().strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    def argv(self, n: int = 1) -> list[str]:
        return (self.dir / f"argv-{n}").read_text(encoding="utf-8").splitlines()

    def stdin(self, n: int = 1) -> str:
        return (self.dir / f"stdin-{n}").read_text(encoding="utf-8")
