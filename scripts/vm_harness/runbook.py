"""The shared parts of the VM runbooks-as-code (``journey8.py`` for build-plan step 8, ``threec10.py`` for 3c-10).

Host side, stdlib only. Nothing here boots a VM or reaches a vendor: it takes a ``guest(argv, timeout) -> stdout`` callable
(``vm_run.make_transport``'s, or a test's fake) and a ``(argv, stdin_text, timeout)`` runner for the one step that must
hand a secret over without putting it in an argv.

The rule these runbooks exist to keep (S1 BUILD_PLAN row 8): every step's RAW line matches its SINGLE expected verdict, and
the verdicts are listed per step and per OS BEFORE the run, with no "or" verdict. So:

* a :class:`Verdict` is one exit code and one regex that must ``fullmatch`` the one raw line; a ``|`` in the regex is
  refused (:func:`check_pattern`), so no verdict can say "this or that";
* the table is written to ``EXPECTED.txt`` (O_EXCL, fsynced, with its sha256) before the first step runs, and
  ``RESULT.txt`` names that digest, so a run cannot quietly edit its expectations afterwards;
* a step whose probe or verdict is not written is ``UNBOUND``, never a pass; a run with any unbound step ends
  ``RESULT: INCOMPLETE``, not ``PASS``.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

RC_MARK = "JOURNEY-RC"
#: ``"$@"`` runs the probe argv with no shell parsing of its words; stderr joins stdout so a refusal (which levain writes to
#: stderr) is a raw line; the exit code is printed on the last line, so a failing probe is data, not a transport error. A
#: newline is printed first, so a probe whose output lacks a trailing newline cannot glue its last line to the marker.
CAPTURE_ARGV = ("/bin/sh", "-c", '"$@" 2>&1; rc=$?; printf "\\n"; echo "' + RC_MARK + ' $rc"', "sh")

Guest = Callable[..., str]
StdinRunner = Callable[[Sequence[str], str, float], str]


class RunbookError(RuntimeError):
    """The runbook itself is wrong (a bad table, an evidence dir that is not fresh): raised before any guest call."""


@dataclass(frozen=True)
class Verdict:
    """ONE expected outcome of one probe: the exit code and the regex the selected raw line must fullmatch."""
    rc: int | None            # None: the exit code is not part of this verdict (a doctor run exits 1 for unrelated rows)
    pattern: str

    def check(self) -> None:
        check_pattern(self.pattern)


def check_pattern(pattern: str) -> None:
    if not pattern or "|" in pattern:
        raise RunbookError(f"a verdict is ONE pattern, no alternation (S1 row 8: no 'or' verdicts): {pattern!r}")
    try:
        re.compile(pattern)
    except re.error as e:
        raise RunbookError(f"verdict pattern does not compile: {pattern!r}: {e}") from None


def capture(guest: Guest, argv: Sequence[str], timeout: float = 120) -> tuple[list[str], int]:
    """Run ``argv`` in the guest; ``(its output lines, its exit code)``. A nonzero exit is returned, not raised."""
    out = guest([*CAPTURE_ARGV, *(str(a) for a in argv)], timeout)
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[-1].startswith(RC_MARK + " "):
        raise RunbookError(f"the guest did not print its exit-code line (last line {lines[-1:]!r})")
    try:
        rc = int(lines[-1].split()[1])
    except (IndexError, ValueError):
        raise RunbookError(f"unreadable exit-code line {lines[-1]!r}") from None
    body = lines[:-1]
    while body and not body[-1].strip():      # the wrapper's own newline (and any blank tail) is not probe output
        body.pop()
    return body, rc


def pick_line(lines: Sequence[str], select: str | None) -> str | None:
    """The raw line a verdict is checked against: the first line that ``search``es ``select``, or, with no ``select``, the
    last non-empty line. None when nothing qualifies (the step then FAILs with an empty raw)."""
    if select is None:
        for ln in reversed(lines):
            if ln.strip():
                return ln.rstrip()
        return None
    rx = re.compile(select)
    for ln in lines:
        if rx.search(ln):
            return ln.rstrip()
    return None


def judge(lines: Sequence[str], rc: int, verdict: Verdict, select: str | None = None) -> tuple[bool, str]:
    """``(matched, raw)``: matched only when the exit code AND the one selected raw line both equal the verdict."""
    raw = pick_line(lines, select)
    return (raw is not None and (verdict.rc is None or rc == verdict.rc) and re.fullmatch(verdict.pattern, raw) is not None), raw or ""


def render_placeholders(argv: Iterable[str], **values: str) -> tuple[str, ...]:
    """Fill {name} words in a probe argv template; a name that is absent or empty is an error, never a literal {x} or an
    empty argument sent to a guest."""
    out = []
    for a in argv:
        for _lit, name, _spec, _conv in string.Formatter().parse(a):
            if name is not None and not values.get(name):
                raise RunbookError(f"probe word {a!r} needs --{name} (not given)")
        out.append(a.format(**values))
    return tuple(out)


# ---- evidence ----------------------------------------------------------------------------------------------------

def _write_durable(path: Path, text: str, *, append: bool = False, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | (os.O_EXCL if exclusive else 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    try:
        data = text.encode()
        while data:
            n = os.write(fd, data)
            if n <= 0:
                raise OSError("short write: os.write made no progress")
            data = data[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


class Evidence:
    """A FRESH directory: ``EXPECTED.txt`` first (before any guest call), ``steps.log`` appended per step, ``RESULT.txt`` last."""

    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise RunbookError(f"evidence dir {self.dir} does not exist (create it empty)")
        if any(self.dir.iterdir()):
            raise RunbookError(f"evidence dir {self.dir} is not empty: a run is scoped to a fresh one")
        self.digest = ""

    def write_expected(self, text: str) -> str:
        self.digest = hashlib.sha256(text.encode()).hexdigest()
        try:
            _write_durable(self.dir / "EXPECTED.txt", text + f"\nsha256: {self.digest}\n", exclusive=True)
        except FileExistsError:
            raise RunbookError(f"evidence dir {self.dir} was claimed by another run") from None
        return self.digest

    def log(self, line: str) -> None:
        _write_durable(self.dir / "steps.log", line + "\n", append=True)

    def write_result(self, text: str) -> Path:
        p = self.dir / "RESULT.txt"
        _write_durable(p, text, exclusive=True)
        return p


def overall(statuses: Sequence[str], omitted: Sequence[str] = ()) -> str:
    """REFUSED beats FAIL beats INCOMPLETE beats PARTIAL beats PASS. ``statuses`` are per-step words; SKIP and NA count as
    neither pass nor fail; UNBOUND and ERROR (a step that timed out or whose transport broke, no verdict observed) make the
    run INCOMPLETE. ``omitted`` are the step ids a ``--step`` subset left out: with any, the word is never plain PASS."""
    if any(s == "REFUSED" for s in statuses):
        return "REFUSED"
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s in ("UNBOUND", "ERROR") for s in statuses):
        return "INCOMPLETE"
    if not any(s == "PASS" for s in statuses):
        return "INCOMPLETE"       # nothing ran: not a pass
    return "PARTIAL" if omitted else "PASS"


# ---- the stdin transport (a secret never goes in an argv) ----------------------------------------------------------

def make_stdin_runner(vm: str, profile, askpass_dir: Path, transport: str) -> StdinRunner:
    """``run(argv, stdin_text, timeout) -> stdout`` to the guest over ``transport``, the one ``vm_run.make_transport`` ACTUALLY
    selected (``guest.kind``: ``"ssh"`` or ``"tart_exec"``, so a linux profile whose guest agent did not answer gets ssh, as
    its ``guest`` does), never the static profile. ssh sends one quoted command (as ``vm_run.make_ssh`` does); ``tart_exec``
    uses ``tart exec -i``. A nonzero exit raises ``GuestError`` with the stderr tail, never the stdin."""
    from scripts.vm_harness import vm_run
    from scripts.vm_harness.driver import GuestError

    vm_run._clone_guard(vm)
    if transport not in ("ssh", "tart_exec"):
        raise RunbookError(f"unknown transport {transport!r}")
    if transport == "tart_exec":
        def run(argv, text, timeout=120):
            r = subprocess.run(vm_run.tart_exec_argv(vm, argv, stdin=True), input=text, capture_output=True, text=True,
                               timeout=timeout)
            if r.returncode:
                raise GuestError(r.returncode, f"guest {list(argv)[:4]}: {r.stderr.strip()[-300:]}")
            return r.stdout
        return run
    askpass = Path(askpass_dir) / "askpass-stdin.sh"
    askpass.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(profile.ssh_password)}\n")
    os.chmod(askpass, 0o700)
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="x")

    cached: list[str] = []

    def run(argv, text, timeout=120):
        from scripts.vm_harness.driver import GuestError
        if not cached:
            out = subprocess.run([vm_run.TART, "ip", vm, "--wait", "60"], capture_output=True, text=True, timeout=90,
                                 check=True).stdout.strip().splitlines()
            if not out:
                raise GuestError(1, f"tart ip {vm} printed nothing")
            cached.append(out[-1])
        ip = cached[0]
        r = subprocess.run([vm_run.SSH, *vm_run.SSH_OPTS, f"{profile.ssh_user}@{ip}", shlex.join(str(a) for a in argv)],
                           input=text, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode:
            raise GuestError(r.returncode, f"guest {list(argv)[:4]}: {r.stderr.strip()[-300:]}")
        return r.stdout
    return run
