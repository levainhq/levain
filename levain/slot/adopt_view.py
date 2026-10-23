"""The operator's side of adopt: ``levain session adopt|discard|restore <session>`` (S1 session 5 DESIGN §1 steps
13-14, §4.6, §4.8, §6 setup, §6.2; CONTRACT §10.3), routed from :func:`levain.slot.launch.main`.

``adopt <session> --repo PATH [--take head|wip|none]``, as the operator (no ``--take`` = the prompt after the
preview, :func:`ask_take`, when stdin is a terminal; a script must say, so no ``--take`` and no terminal =
``bad-argument`` before any helper call):

1. ``status <session>`` through the runtime helper: the alloc's ``base`` (none = ``base-unrecorded``).
2. :func:`check_repo`, BEFORE adopt-open, so a refusal costs no sealed run, and again immediately before the take
   writes his repo, in this order: ``repo-not-a-root`` (``--repo/.git`` is not a directory: a subdirectory, a
   linked worktree or a submodule checkout, whose ``.git`` is a file; that directory is the whole check), then the
   config reads: a set ``extensions.compatObjectFormat`` = ``object-format-mismatch`` and ``extensions.partialClone``
   or a ``remote.<name>.partialCloneFilter`` = ``repo-partial`` (whichever git lists first), then a
   ``remote.<name>.promisor`` git reads as true = ``repo-partial``; then ``object-format-mismatch``
   (``rev-parse --show-object-format`` against the base's), ``repo-shallow``, and ``base-missing``
   (``cat-file -e <base_oid>``). Every value is git's own typing; an empty or invalid ``.git``
   is ``repo-unreadable:128`` (every git call names the repository, :func:`adopt_take.repo_args`).
3. ``adopt-open <session>`` ("waiting on a running import" is printed first: root's adopt-lock wait is bounded,
   §4.7); its answer checked against the alloc's base. ``adopt-nothing-new`` copies the report and shows it
   (:func:`render_report_only`). Every helper call runs within :func:`helper_timeout` of its verb, in its own
   bounded wait (:func:`_run`), and every answer's ``warnings`` are printed (:func:`show_warnings`).
4. The private copies (§6 setup): a 0700 dir under ``$TMPDIR``; the published ``bundle`` and ``report.json``, then
   (after ``adopt-verify``) ``verify/verified.json``, ``verify/q.idx`` and ``verify/preview.txt``, each opened under
   the export dir's fd (``O_DIRECTORY|O_NOFOLLOW``, then ``O_NOFOLLOW|O_NONBLOCK``), regular, owner root, nlink 1,
   within its cap, hashed while copied, the digest equal to root's answer: any of it failing = ``export-changed``.
   Before the bundle copy, free space in that dir must hold the bundle, the idx bound from the pack header's object
   count and :data:`PRIVATE_MARGIN` (``preview-space``); an ENOSPC or EDQUOT while copying is ``preview-space`` too.
5. :func:`render`: the preview the operator reads. Every line of the verifier's ``preview.txt`` is re-escaped by
   :func:`adopt_preview.escape` (the verifier's FSI / PDI isolates allowed, every other unshowable character marked),
   since the confined verifier read hostile bytes; the importer's report by :func:`adopt_preview.report_section`;
   the checkout path (his ``--repo``) and the private dir's path.
6. ``head`` or ``wip`` (``--take``, or his answer): :func:`adopt_take.take` over the private copies; ``none``
   takes nothing. An adopt that fails before its preview was shown removes the whole private dir; once shown, only
   ``bundle`` and ``q.idx`` are removed and the dir is printed with the one command that removes it
   (:func:`removal_line`).

His git runs only on trusted input, each spawn through :func:`_git` (``-c core.hooksPath=/dev/null``,
:func:`adopt_take.repo_args`, :func:`adopt_take.git_env`, ``env=`` given): ``config --local --includes
--name-only --get-regexp`` and ``config --local --includes --type=bool --get-regexp`` (the partial-clone reads),
``rev-parse --show-object-format``, ``rev-parse --is-shallow-repository``, ``cat-file -e <base_oid>``; the take adds
``cat-file -e`` per
prerequisite, ``rev-parse --git-path objects`` and ``update-ref``. A git that cannot start or outlives
:data:`REPO_GIT_TIMEOUT` = ``repo-unreadable:<errno name>`` / ``repo-unreadable:timeout``; one that answers the
config read or ``rev-parse`` with an rc it has no meaning for = ``repo-unreadable:<rc>``.

``discard <session>`` / ``restore <session>``: the root verb of the same name, its answer printed (§4.6).
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import inspect
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import canon
from levain.slot import Refused, adopt_preview, adopt_take, adopt_verify, bundlefmt
from levain.slot.grammar import SESSION_RE

TAKES = ("head", "wip", "none")
STORE_VERBS = ("discard", "restore")
VERBS = ("adopt",) + STORE_VERBS
#: His git's wall for one trusted-input call before adopt-open (adopt_take's own bound for its calls).
REPO_GIT_TIMEOUT = 120
#: The fixed margin of the CLI side's space check (§6 setup, A9) [judged by adopt-s4a, 10-23, against the
#: verifier image's own fixed margin, §6.3].
PRIVATE_MARGIN = 64 << 20
#: The export class an operator adopt publishes into (root's ``runtime_helper.ADOPT_CLASS``).
EXPORT_CLASS = "operator"
#: adopt-open's results (§4.5; the importer's three, ``importer`` module docstring).
RESULTS = ("delta", "unrelated", "adopt-nothing-new")
_CHUNK = 1 << 20
#: The keys whose presence alone refuses (git prints section and key lowercased; read ``--name-only``).
PRESENCE_REGEXP = r"^(extensions\.(partialclone|compatobjectformat)|remote\..*\.partialclonefilter)$"
#: The promisor keys, read ``--type=bool`` (a bare ``promisor`` line is git's true).
PROMISOR_REGEXP = r"^remote\..*\.promisor$"

Log = Optional[Callable[[str], None]]


def _note(log: Log, line: str) -> None:
    if log is not None:
        log(line)


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, "EIO")


# --- the runtime helper ---------------------------------------------------------------------------------------


def _run(argv: Sequence[str], *, env: Mapping[str, str], timeout: float, stdin: object = subprocess.DEVNULL,
         cwd: Optional[str] = None, signal_child: bool = True,
         **kw: object) -> "subprocess.CompletedProcess[bytes]":
    """Every child this module starts: ``env`` is required (the spawn scan, ``tests/test_launch.py``). The child stays
    in the operator's session and tty (a ``requiretty`` / ``tty_tickets`` sudoers policy that works from his shell
    works here). Every wait is bounded; past ``timeout``, or on an interrupt, :func:`_stop` ends the wait and then
    ``TimeoutExpired`` (or the interrupt) is raised. ``signal_child`` picks the stop: True (his own git, :func:`_git`)
    = SIGTERM, :data:`STOP_GRACE_S`, SIGKILL, one more bounded wait; False (the root helper, :func:`call_helper`) = NO
    signal: the CLI closes its pipe ends and stops waiting. The root helper is built to outlive the operator's
    signals (``runtime_helper.wait_child``: "the helper must outlive it to answer its one line"), because it alone
    runs the bounded collect and seal-out of its sealed child; a SIGTERM relayed by ``sudo`` would kill it before that
    cleanup. A closed pipe can cut only its answer: ``runtime_helper.main`` writes the line after ``run`` returns,
    and each verb's seal-out runs inside ``run`` (its ``finally``)."""
    stdout = kw.pop("stdout", subprocess.PIPE)
    stderr = kw.pop("stderr", subprocess.PIPE)
    if kw.pop("capture_output", False):
        stdout = stderr = subprocess.PIPE
    if kw:
        raise TypeError(f"_run: {sorted(kw)}")
    p = subprocess.Popen(list(argv), env=dict(env), stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:           # the timeout, and an interrupt: stop waiting, bounded
        _stop(p, signal_child=signal_child)
        raise
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


#: After a timeout's SIGTERM to his own git, how long the CLI waits for it before SIGKILL, and again after the SIGKILL
#: [judged by adopt-s4a-fix2, 10-23, against git's own cleanup being lock-file unlinks].
STOP_GRACE_S = 10.0


def _stop(p: "subprocess.Popen[bytes]", *, signal_child: bool) -> None:
    for f in (p.stdin, p.stdout, p.stderr):
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
    if not signal_child:            # the root helper: no signal, no wait (it answers into a closed pipe, or not at all)
        return
    for send in (p.terminate, p.kill):
        try:
            send()
        except OSError:             # already reaped: nothing left to signal
            return
        try:
            p.wait(timeout=STOP_GRACE_S)
            return
        except subprocess.TimeoutExpired:   # TERM ignored: KILL; KILL not reaped in the grace (a D-state git): stop
            pass


#: The setup slack of one helper verb beyond its own named bounds: root's copies-in, hashes and publish around the
#: sealed run [judged by adopt-s4a-fix1, 10-23, against two passes over V7_BYTES of bundle + base at disk speed].
HELPER_SLACK_S = 900.0
#: The slack of ``status`` / ``discard`` / ``restore``: sudo, the helper's start and its file reads, renames and
#: fsyncs, none of which copies a bundle [judged by adopt-s4b, 10-23, against ``store_verbs`` doing renames and
#: stamp writes only].
STORE_SLACK_S = 60.0
#: CAS calls one ``discard`` / ``restore`` can make: a journal of the other verb finished forward first
#: (``store_verbs._finish_journal``), then its own commit.
STORE_VERB_CAS_CALLS = 2


def _control_bounds() -> Tuple[float, float]:
    """CONTROL's per-answer bound (``ControlClient``'s ``timeout``, which also bounds the connect) and its ``locked``
    retry window (V23), read from the client the helper uses."""
    from levain.slot.control_client import ControlClient

    return (float(inspect.signature(ControlClient.__init__).parameters["timeout"].default),
            float(ControlClient.retry_window_s))


def helper_timeout(verb: str, *, bundle_bytes: int = 0, base_bytes: int = 0) -> float:
    """The CLI's wall for one helper verb, from the helper's own bounds (never a fixed number): ``adopt-open`` =
    its one deadline over the adopt-lock wait and the walk (``AdoptSeams.lock_wait_s``) + ``RESULT_GRACE_S``;
    ``adopt-verify`` = its lock-wait deadline (``VerifySeams.lock_wait_s``) + V30's wall for these sizes
    (``verify_wall_s``) + ``RESULT_GRACE_S``; each + :data:`HELPER_SLACK_S`. ``status`` reads files only (no lock, no
    CONTROL call); ``discard`` / ``restore`` TRY the session lock (``session-busy``, no wait) and make at most
    :data:`STORE_VERB_CAS_CALLS` CAS calls, each bounded by CONTROL's connect / answer bound and its ``locked``
    window (:func:`_control_bounds`); each + :data:`STORE_SLACK_S`."""
    from levain.slot import runtime_helper as RH

    if verb == "adopt-open":
        return RH.AdoptSeams().lock_wait_s + RH.RESULT_GRACE_S + HELPER_SLACK_S
    if verb == "adopt-verify":
        return (RH.VerifySeams().lock_wait_s + RH.verify_wall_s(bundle_bytes, base_bytes) + RH.RESULT_GRACE_S
                + HELPER_SLACK_S)
    if verb in STORE_VERBS:
        answer, window = _control_bounds()
        return answer + STORE_VERB_CAS_CALLS * (window + answer) + STORE_SLACK_S
    if verb == "status":
        return STORE_SLACK_S
    raise ValueError(f"helper_timeout: {verb!r}")


@dataclass
class Seams:
    """The CLI's OS facts. ``helper`` is the argv prefix of the root runtime helper (``sudo -n <path>``);
    ``store`` is root's store ``L``; ``root_uid`` the owner every published file must have; ``tmpdir`` the parent of
    the private dir (None = ``$TMPDIR``); ``git`` his git."""

    helper: Optional[Tuple[str, ...]] = None
    store: Optional[Path] = None
    root_uid: int = 0
    tmpdir: Optional[Path] = None
    git: Optional[str] = None
    helper_timeout_s: Optional[float] = None        # None = :func:`helper_timeout` per verb (a rig sets a wall)
    log: Log = None
    stdin: Optional[IO[str]] = None                 # his answer to the prompt (None = ``sys.stdin``)
    spawn: Callable[..., "subprocess.CompletedProcess[bytes]"] = field(default_factory=lambda: _run)

    def __post_init__(self) -> None:
        from levain.slot import layout

        host = "darwin" if sys.platform == "darwin" else "linux"
        if self.helper is None:
            self.helper = ("sudo", "-n", str(layout.runtime_helper_path(host)))   # type: ignore[arg-type]
        if self.store is None:
            self.store = layout.STORE[host]                                         # type: ignore[index]


def call_helper(seams: Seams, *args: str, err: IO[str], timeout: Optional[float] = None) -> Dict[str, object]:
    """One runtime-helper verb, stdin empty, within ``timeout`` (None = :func:`helper_timeout` of the verb;
    ``seams.helper_timeout_s`` overrides both); its stderr shown escaped (root's detail may quote a session-made
    name), its stdout one canonical line (:func:`supervisor.parse_helper_line`); an answer's ``warnings`` printed
    to ``err`` (:func:`show_warnings`)."""
    from levain.launch import child_env
    from levain.slot.supervisor import parse_helper_line

    argv = [*seams.helper, *args]                                                  # type: ignore[misc]
    wall = seams.helper_timeout_s if seams.helper_timeout_s is not None else (
        timeout if timeout is not None else helper_timeout(args[0]))
    _note(seams.log, "helper " + " ".join(args))
    try:
        p = seams.spawn(argv, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=wall, signal_child=False)
    except subprocess.TimeoutExpired:
        raise Refused("helper-timeout", f"{args[0]} ran past {wall:.0f} s; the CLI stopped waiting and signalled "
                                        "nothing, and root's own wall reaps its sealed child") from None
    except OSError as e:
        raise Refused("helper-unrunnable", f"{args[0]}: {e.strerror or e}") from None
    for line in (p.stderr or b"").splitlines()[-5:]:
        if line.strip():
            err.write("  " + adopt_preview.escape(line.strip()) + "\n")
    ans = parse_helper_line(p.stdout or b"", args[0])
    show_warnings(ans, args[0], err)
    return ans


#: The cap on the warnings one answer may carry, and on each one's length, before the answer is malformed
#: [judged by adopt-s4a-fix1, 10-23, against root's warnings being one line per post-commit fsync failure].
WARNINGS_MAX = 16
WARNING_CHARS = 1000


def show_warnings(ans: Mapping[str, object], verb: str, err: IO[str]) -> List[str]:
    """An answer's ``warnings`` (root's post-commit failures, A2): a list of strings, else
    ``adopt-answer-malformed``; each printed escaped on its own ``WARNING`` line. Returns them escaped."""
    w = ans.get("warnings", [])
    if not isinstance(w, list) or len(w) > WARNINGS_MAX or any(
            not isinstance(x, str) or len(x) > WARNING_CHARS for x in w):
        raise Refused("adopt-answer-malformed", f"{verb}: warnings")
    try:
        shown = [adopt_preview.escape(x.encode("utf-8", "surrogateescape")) for x in w]
    except UnicodeEncodeError:      # a lone surrogate outside surrogateescape's U+DC80-DCFF
        raise Refused("adopt-answer-malformed", f"{verb}: warnings") from None
    for x in shown:
        err.write(f"WARNING ({verb}): {x}\n")
    if shown:
        err.flush()
    return shown


# --- the --repo checks (§4.8, A17) -----------------------------------------------------------------------------


def _git(seams: Seams, repo: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    argv = [seams.git or "git", "-c", "core.hooksPath=/dev/null", *adopt_take.repo_args(repo), *args]
    _note(seams.log, "spawn " + " ".join(argv[1:]))
    try:
        return seams.spawn(argv, cwd=str(repo), env=adopt_take.git_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=REPO_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("repo-unreadable:timeout", f"git {args[0]} ran past {REPO_GIT_TIMEOUT} s") from None
    except OSError as e:
        raise Refused(f"repo-unreadable:{_errno_name(e)}", f"git {args[0]}: {e.strerror or e}") from None


def _stderr(r: "subprocess.CompletedProcess[bytes]") -> str:
    return adopt_preview.escape((r.stderr or b"").strip()[-300:])


def check_base(base: object) -> Mapping[str, object]:
    """The alloc's ``base`` as the status answer gave it: an oid of its object format."""
    if not isinstance(base, dict):
        raise Refused("base-unrecorded", "the session's alloc records no base")
    fmt, oid = base.get("object_format"), base.get("oid")
    if fmt not in bundlefmt.OID_HEX or not isinstance(oid, str) or len(oid) != bundlefmt.OID_HEX[fmt] \
            or oid.strip("0123456789abcdef"):
        raise Refused("adopt-answer-malformed", "status: base")
    return base


def check_repo(seams: Seams, repo: Path, base: Mapping[str, object]) -> Path:
    """§4.8's checks, in the order the module docstring's step 2 lists, before adopt-open and again before the take.
    Returns ``repo`` made absolute."""
    repo = Path(os.path.abspath(repo))
    try:
        st = os.lstat(repo / ".git")
    except FileNotFoundError:
        raise Refused("repo-not-a-root", f"{adopt_preview.escape(os.fsencode(repo))} holds no .git directory: "
                                         "give the top of the work tree") from None
    except OSError as e:
        raise Refused(f"repo-unreadable:{_errno_name(e)}", str(e)) from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("repo-not-a-root", f"{adopt_preview.escape(os.fsencode(repo))}/.git is not a directory (a "
                                         "linked worktree, a submodule or a link): give the main work tree")
    # Every value below is typed by his git, never parsed here (L3 s4a r1 complement MED 1 + LOW 3): the keys whose
    # mere presence refuses by name only, the promisor keys as git's own booleans, the format by rev-parse; each read
    # follows include / includeIf, as the take's own git commands do.
    r = _git(seams, repo, "config", "--local", "--includes", "--name-only", "--get-regexp", PRESENCE_REGEXP)
    if r.returncode not in (0, 1):
        # His git cannot open the repository (git 2.54 without Rust refuses extensions.compatObjectFormat here:
        # s4a_run.txt row R5b; an empty or invalid .git is "not a git repository" under repo_args): the take could
        # not run either.
        raise Refused(f"repo-unreadable:{r.returncode}", f"git config: {_stderr(r)}")
    for key in (r.stdout or b"").decode("utf-8", "surrogateescape").splitlines():
        shown = adopt_preview.escape(key.encode("utf-8", "surrogateescape"))
        if key.lower() == "extensions.compatobjectformat":
            raise Refused("object-format-mismatch", "extensions.compatObjectFormat is set: the take writes no object "
                                                    "map")
        raise Refused("repo-partial", f"{shown} is set")
    r = _git(seams, repo, "config", "--local", "--includes", "--type=bool", "--get-regexp", PROMISOR_REGEXP)
    if r.returncode not in (0, 1):      # a promisor value git cannot read as a boolean: git's own refusal
        raise Refused(f"repo-unreadable:{r.returncode}", f"git config --type=bool: {_stderr(r)}")
    for line in (r.stdout or b"").decode("utf-8", "surrogateescape").splitlines():
        key, _, value = line.rpartition(" ")      # git prints "<key> true|false" (a subsection may hold a space)
        shown = adopt_preview.escape(key.encode("utf-8", "surrogateescape"))
        if value == "true":
            raise Refused("repo-partial", f"{shown} is true")
        if value != "false":
            raise Refused("repo-unreadable:config", "git config --type=bool answered "
                          + adopt_preview.escape(line.encode("utf-8", "surrogateescape")))
    r = _git(seams, repo, "rev-parse", "--show-object-format")
    fmt = (r.stdout or b"").decode("ascii", "replace").strip()
    if r.returncode != 0 or fmt not in bundlefmt.OID_HEX:
        raise Refused(f"repo-unreadable:{r.returncode}", "git rev-parse --show-object-format (needs git >= 2.38): "
                                                         f"{_stderr(r)}")
    if fmt != base["object_format"]:
        raise Refused("object-format-mismatch", f"his repository is {adopt_preview.escape(fmt.encode())}, the "
                                                f"session's base is {base['object_format']}")
    r = _git(seams, repo, "rev-parse", "--is-shallow-repository")
    out = (r.stdout or b"").strip()
    if r.returncode != 0 or out not in (b"true", b"false"):
        raise Refused(f"repo-unreadable:{r.returncode}", f"git rev-parse: {_stderr(r)}")
    if out == b"true":
        raise Refused("repo-shallow", "a shallow repository cannot hold the session's history")
    r = _git(seams, repo, "cat-file", "-e", str(base["oid"]))
    if r.returncode != 0:
        raise Refused("base-missing", f"{base['oid']} is not in {adopt_preview.escape(os.fsencode(repo))}")
    return repo


# --- the private copies (§6 setup) -----------------------------------------------------------------------------


def _changed(detail: str) -> Refused:
    return Refused("export-changed", detail)


def _digest(ans: Mapping[str, object], key: str) -> str:
    d = ans.get(key)
    if not isinstance(d, str) or not d.startswith("sha256:") or len(d) != 71 or d[7:].strip("0123456789abcdef"):
        raise Refused("adopt-answer-malformed", key)
    return d[7:]


def open_dir(seams: Seams, path: Path, dfd: Optional[int] = None) -> int:
    """A root-owned published dir: ``O_DIRECTORY|O_NOFOLLOW`` (under ``dfd`` by name when given)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     **({"dir_fd": dfd} if dfd is not None else {}))
    except OSError as e:
        raise _changed(f"{path}: {e.strerror}") from None
    st = os.fstat(fd)
    if st.st_uid != seams.root_uid:
        os.close(fd)
        raise _changed(f"{path}: owner {st.st_uid}, not root")
    return fd


def open_published(seams: Seams, dfd: int, name: str, cap: int) -> Tuple[int, int]:
    """``name`` under ``dfd``: ``O_NOFOLLOW|O_NONBLOCK``, regular, owner root, nlink 1, within ``cap``. Returns
    (fd, size)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as e:
        raise _changed(f"{name}: {e.strerror}") from None
    st = os.fstat(fd)
    why = ("not a regular file" if not stat.S_ISREG(st.st_mode) else f"owner {st.st_uid}, not root"
           if st.st_uid != seams.root_uid else f"nlink {st.st_nlink}" if st.st_nlink != 1
           else f"{st.st_size} bytes > {cap}" if st.st_size > cap else None)
    if why is not None:
        os.close(fd)
        raise _changed(f"{name}: {why}")
    return fd, st.st_size


def copy_private(src: int, size: int, dst: Path, want: str) -> Path:
    """``src`` (``size`` bytes) into a new 0600 ``dst``, hashed while copied; the sha256 must equal ``want``."""
    sha = hashlib.sha256()
    out = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    done = False
    try:
        pos = 0
        while pos < size:
            b = os.pread(src, min(_CHUNK, size - pos), pos)
            if not b:
                raise _changed(f"{dst.name}: shrank while copied")
            sha.update(b)
            view = memoryview(b)
            try:
                while view:
                    view = view[os.write(out, view):]
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise Refused("preview-space", f"{dst.parent}: {e.strerror} while copying {dst.name}") from None
                raise
            pos += len(b)
        if os.pread(src, 1, size):
            raise _changed(f"{dst.name}: grew while copied")
        if sha.hexdigest() != want:
            raise _changed(f"{dst.name}: its sha256 is not the digest root answered")
        done = True
    finally:
        os.close(out)
        if not done:        # an incomplete or unauthenticated copy never stays behind (L3 s4a r1 codex MED 7)
            try:
                os.unlink(dst)
            except FileNotFoundError:
                pass
    return dst


def private_dir(seams: Seams) -> Path:
    """A fresh 0700 dir under his ``$TMPDIR`` (``tempfile.mkdtemp`` makes it 0700, by name, ``O_EXCL``)."""
    return Path(tempfile.mkdtemp(prefix="levain-adopt-", dir=str(seams.tmpdir) if seams.tmpdir else None))


def free_bytes(path: Path) -> int:
    v = os.statvfs(str(path))
    return v.f_bavail * v.f_frsize


def bundle_space(src: int, size: int, object_format: str) -> Tuple[int, int]:
    """The CLI side's need (§6 setup, A9) from the PUBLISHED bundle's header and pack header, read through the fd
    the copy then reads: ``bundle + idx bound + PRIVATE_MARGIN``. Returns (need, object count)."""
    from levain.slot.runtime_helper import idx_bound

    h = adopt_verify.read_header_at(src)
    if h.object_format != object_format:
        raise Refused(bundlefmt.MISMATCH, f"a {h.object_format} bundle for a {object_format} base")
    objects = adopt_verify.pack_count_at(src, h.size, size - h.size)
    return size + idx_bound(objects, object_format) + PRIVATE_MARGIN, objects


# --- the view (§6.2) -------------------------------------------------------------------------------------------


#: The copies this command deletes when it ends; every other file in the private dir stays for him to re-read.
LARGE_COPIES = ("bundle", "q.idx")


def removal_line(private: Path) -> str:
    """What the end of this command does to the private dir and the one command that removes the rest, printed
    beside its path (S4a Q6 as ruled by the head 1023+13: the dir stays so he can re-read the preview; no verb, no
    flag). The files named are the ones in the dir NOW. A path the escaper would change is not offered as a command."""
    try:
        names = sorted(p.name for p in private.iterdir())
    except OSError:
        names = []
    gone = [n for n in LARGE_COPIES if n in names]
    keep = [adopt_preview.escape(os.fsencode(n)) for n in names if n not in LARGE_COPIES]
    head = (f"the {' and '.join(gone)} copies will be removed when this command ends; " if gone else "")
    head += f"these remain: {', '.join(keep) or 'nothing'}; "
    shown = adopt_preview.escape(os.fsencode(private))
    if shown != str(private):
        return head + "remove the directory by rm -r (its name holds characters shown escaped above)"
    return head + f"remove the directory with: rm -r -- {shlex.quote(str(private))}"


def render(*, session: str, result: str, refs: Sequence[str], preview: bytes, report: Mapping[str, object],
           repo: Path, private: Path) -> List[str]:
    """The preview the operator reads: the A4 header, his checkout path, the verifier's preview re-escaped line by
    line, the importer's report (a proposal), the refs he can take and where the private copies are."""
    out = [adopt_verify.PREVIEW_HEADER,
           f"session {session}: {result}; it would be taken into {adopt_preview.escape(os.fsencode(repo))}", ""]
    allow = adopt_preview.FSI + adopt_preview.PDI
    lines = preview.split(b"\n")
    if lines[:1] == [adopt_verify.PREVIEW_HEADER.encode()]:     # the CLI's own copy of it is printed above
        lines = lines[1:]
    out += [adopt_preview.escape(ln, allow=allow) for ln in lines]
    if out[-1] == "":
        out.pop()
    out.append("")
    out += adopt_preview.report_section(report)
    out.append("")
    out.append("refs you can take: " + (", ".join(f"{r} -> {adopt_take.ref_name(session, r)}"
                                                   for r in refs) or "none"))
    out.append(f"private copies: {adopt_preview.escape(os.fsencode(private))}")
    out.append(removal_line(private))
    return out


def render_report_only(*, session: str, report: Mapping[str, object], repo: Path, private: Path) -> List[str]:
    """``adopt-nothing-new``'s view: nothing to take, and the importer's report (a proposal) all the same."""
    out = [f"session {session}: nothing new since its base; nothing to take into "
           f"{adopt_preview.escape(os.fsencode(repo))}.", ""]
    out += adopt_preview.report_section(report)
    out.append("")
    out.append(f"private copies: {adopt_preview.escape(os.fsencode(private))}")
    out.append(removal_line(private))
    return out


#: The longest answer read at the prompt; a longer one is drained to its newline and asked again.
ANSWER_MAX = 64


def is_tty(stream: object) -> bool:
    """A stream that is a terminal. None, a closed stream or one without ``isatty`` is not."""
    try:
        return bool(stream is not None and stream.isatty())       # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def ask_take(refs: Sequence[str], stdin: IO[str], out: IO[str]) -> str:
    """The interactive choice after the preview: the refs the export carries, then ``none``. Anything else asks
    again (an answer that does not decode, or runs past :data:`ANSWER_MAX`, included); end of input = ``none``."""
    choices = [r for r in ("head", "wip") if r in refs] + ["none"]
    prompt = "take " + " / ".join(choices) + "? "
    while True:
        out.write(prompt)
        out.flush()
        try:
            line = stdin.readline(ANSWER_MAX)
            if len(line) >= ANSWER_MAX and not line.endswith("\n"):
                while True:                 # too long: drain to its newline; the whole logical line is one bad answer
                    more = stdin.readline(ANSWER_MAX)
                    if not more:            # end of input mid-drain = the end-of-input path below
                        line = ""
                        break
                    if more.endswith("\n"):
                        line = "?\n"
                        break
        except UnicodeDecodeError:
            line = "?\n"
        if not line:
            out.write("\n(end of input: none)\n")
            return "none"
        answer = line.strip()
        if answer in choices:
            return answer
        out.write(f"answer one of: {', '.join(choices)}\n")


def ask_or_interrupt(refs: Sequence[str], stdin: IO[str], out: IO[str], err: IO[str]) -> Tuple[Optional[str], int]:
    """:func:`ask_take`, with Ctrl-C at the prompt = (None, 130) and one line: nothing was taken."""
    try:
        return ask_take(refs, stdin, out), 0
    except KeyboardInterrupt:
        err.write("levain session adopt: interrupted; nothing taken\n")
        return None, 130


def verify_line(wall: float) -> str:
    """The one line before ``adopt-verify`` (the S4b L4 run: nothing printed for up to V30's wall)."""
    return (f"verifying in root's confined verify (this can take a while; the CLI waits at most {wall:.0f} s)...\n")


def warn_old_git(seams: "Seams", err: IO[str]) -> None:
    """One ``WARNING`` line when his git is below the maintenance release of its line (Q1; never a refusal). The
    probe is the take's own git, spawned the way the take spawns it (``adopt_take.git_env()``, so the same PATH
    resolution), bounded by ``fsck_ids.VERSION_TIMEOUT``; a git that cannot say its version is warned about as unparsed."""
    from levain.slot import fsck_ids

    try:
        r = seams.spawn([seams.git or "git", "--version"], env=adopt_take.git_env(), stdin=subprocess.DEVNULL,
                        capture_output=True, timeout=fsck_ids.VERSION_TIMEOUT, cwd="/")
        if r.returncode != 0:
            note: Optional[str] = f"git version unparsed (exit {r.returncode})"
        else:
            note = fsck_ids.below_maintenance(r.stdout.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        note = "git version unparsed (no answer in time)"
    except (OSError, ValueError) as e:
        note = f"git version unparsed ({type(e).__name__})"
    if note is not None:
        err.write("WARNING: " + adopt_preview.escape(note.encode()) + "\n")


# --- the verbs --------------------------------------------------------------------------------------------------


def _parser(verb: str) -> argparse.ArgumentParser:
    from levain.slot.launch import _Parser

    p = _Parser(prog=f"levain session {verb}")
    p.add_argument("session")
    if verb == "adopt":
        p.add_argument("--repo", required=True, type=Path, metavar="PATH",
                       help="Your repository: the top of its work tree, holding the session's base commit.")
        p.add_argument("--take", choices=TAKES, default=None,
                       help="head or wip: create refs/heads/levain/<session>[-wip] from the export; none: preview "
                            "only. Without it you are asked after the preview (a terminal is required).")
        p.add_argument("--git", default=None, metavar="GIT", help=argparse.SUPPRESS)
    return p


def _session(text: str) -> str:
    if not SESSION_RE.fullmatch(text):
        raise Refused("bad-argument", f"a session id s-<16 hex>, got {adopt_preview.escape(text.encode())}")
    return text


def _copy_report(seams: Seams, expect: Path, priv: Path, ans: Mapping[str, object]) -> Mapping[str, object]:
    """The published ``report.json`` copied (:func:`open_published`, :func:`copy_private` against root's
    ``report_digest``) and parsed."""
    from levain.slot.importer import REPORT_CAP

    edfd = open_dir(seams, expect)
    try:
        src, size = open_published(seams, edfd, "report.json", REPORT_CAP)
        try:
            copy_private(src, size, priv / "report.json", _digest(ans, "report_digest"))
        finally:
            os.close(src)
    finally:
        os.close(edfd)
    report = canon.parse((priv / "report.json").read_bytes())
    if not isinstance(report, dict):
        raise _changed("report.json: not an object")
    return report


def _base_bytes(base: Mapping[str, object]) -> int:
    """The base bundle's size for V30's wall; a status answer without one = the largest base V7 allows."""
    from levain.slot.runtime_helper import V7_BYTES

    nb = base.get("bundle_bytes")
    return nb if isinstance(nb, int) and not isinstance(nb, bool) and nb >= 0 else V7_BYTES + bundlefmt.HEADER_MAX


def adopt(session: str, repo: Path, which: Optional[str], seams: Seams, out: IO[str], err: IO[str]) -> int:
    """``which`` None = :func:`ask_take` on ``seams.stdin`` after the preview (the caller checked it is a terminal)."""
    rec = call_helper(seams, "status", session, err=err).get("records")
    if not isinstance(rec, list) or len(rec) != 1 or not isinstance(rec[0], dict):
        raise Refused("adopt-answer-malformed", "status: records")
    base = check_base(rec[0].get("base"))
    repo = check_repo(seams, repo, base)
    out.write("waiting on a running import (if one holds the adopt lock)...\n")
    out.flush()
    ans = call_helper(seams, "adopt-open", session, err=err)       # its warnings are printed there
    result = ans.get("result")
    if result not in RESULTS or ans.get("base_oid") != base["oid"] or ans.get("object_format") != \
            base["object_format"]:
        raise Refused("adopt-answer-malformed", "adopt-open: result / base")
    expect = Path(seams.store) / "export" / EXPORT_CLASS / session                 # type: ignore[arg-type]
    if ans.get("repo") != str(expect):
        raise Refused("adopt-answer-malformed", "adopt-open: repo")
    refs = ans.get("refs")
    if not isinstance(refs, list) or any(r not in ("head", "wip") for r in refs):
        raise Refused("adopt-answer-malformed", "adopt-open: refs")
    warned = bool(ans.get("warnings"))
    fmt = str(base["object_format"])
    from levain.slot.runtime_helper import V7_BYTES, VERIFIED_CAP, idx_bound
    from levain.slot.verify_caps import PREVIEW_OUTPUT_CAP

    priv = private_dir(seams)
    bundle_copy = priv / "bundle"
    idx_copy = priv / "q.idx"
    shown = False                   # the private dir is kept only once the operator has been shown what it holds
    try:
        if result == "adopt-nothing-new":
            # A report-only export (L3 s4a r1 codex MED 5): the importer's named entries (a skipped nested .git, a
            # FIFO) are shown, never dropped behind "nothing new".
            report = _copy_report(seams, expect, priv, ans)
            for line in render_report_only(session=session, report=report, repo=repo, private=priv):
                out.write(line + "\n")
            shown = True
            return 0
        if which is not None and which != "none" and which not in refs:
            raise Refused("bad-argument", f"--take {which}: the export carries no {which} ref")
        edfd = open_dir(seams, expect)
        try:
            src, size = open_published(seams, edfd, "bundle", V7_BYTES + bundlefmt.HEADER_MAX)
            try:
                need, objects = bundle_space(src, size, fmt)
                if free_bytes(priv) < need:
                    raise Refused("preview-space", f"{free_bytes(priv)} bytes free in "
                                                   f"{adopt_preview.escape(os.fsencode(priv))} < {need}")
                copy_private(src, size, bundle_copy, _digest(ans, "bundle_digest"))
            finally:
                os.close(src)
        finally:
            os.close(edfd)
        report = _copy_report(seams, expect, priv, ans)
        wall = helper_timeout("adopt-verify", bundle_bytes=size, base_bytes=_base_bytes(base))
        out.write(verify_line(seams.helper_timeout_s if seams.helper_timeout_s is not None else wall))
        out.flush()
        v = call_helper(seams, "adopt-verify", session, "--bundle-digest", "sha256:" + _digest(ans, "bundle_digest"),
                        err=err, timeout=wall)
        warned = warned or bool(v.get("warnings"))
        edfd = open_dir(seams, expect)
        try:
            vdfd = open_dir(seams, Path("verify"), edfd)
        finally:
            os.close(edfd)
        try:
            for name, key, cap in (("verified.json", "verified_digest", VERIFIED_CAP),
                                   ("q.idx", "idx_digest", idx_bound(objects, fmt)),
                                   ("preview.txt", "preview_digest", PREVIEW_OUTPUT_CAP)):
                src, size = open_published(seams, vdfd, name, cap)
                try:
                    copy_private(src, size, priv / name, _digest(v, key))
                finally:
                    os.close(src)
        finally:
            os.close(vdfd)
        claims = canon.parse((priv / "verified.json").read_bytes())
        if not isinstance(claims, dict) or claims.get("result") != result:
            raise Refused("verifier-claim-mismatch", "verified.json: result")
        for line in render(session=session, result=str(result), refs=refs, preview=(priv / "preview.txt").read_bytes(),
                           report=report, repo=repo, private=priv):
            out.write(line + "\n")
        shown = True
        if which is None:
            which, rc = ask_or_interrupt(refs, seams.stdin or sys.stdin, out, err)
            if which is None:
                return rc
        if which == "none":
            out.write("nothing taken (none); " + (
                "root WARNED above that its publish may not survive a crash: the export stays retained only if "
                "that publish did.\n" if warned else "the export stays retained.\n"))
            return 0
        # The checks again, immediately before his repo is written (L3 s4a r1 codex MED 3): the import and verify
        # may have run for an hour, during which his repo could have turned shallow, partial or another format.
        check_repo(seams, repo, base)
        warn_old_git(seams, err)
        t = adopt_take.take(git=seams.git or "git", repo=repo, bundle=bundle_copy, idx=idx_copy, claims=claims,
                            session=session, object_format=fmt, which=which, log=seams.log)
        out.write(f"took {which}: {t.ref} -> {t.oid}\n")
        return 0
    finally:
        if not shown:               # a failed adopt leaves nothing in $TMPDIR (L3 s4a r1 codex MED 7, complement LOW 4)
            shutil.rmtree(priv, ignore_errors=True)
        else:
            for p in (bundle_copy, idx_copy):      # the large copies; the preview, report and claims stay readable
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


def store_verb(verb: str, session: str, seams: Seams, out: IO[str], err: IO[str]) -> int:
    ans = call_helper(seams, verb, session, err=err)
    out.write(f"{verb} {session}: done" + (f" ({adopt_preview.escape(str(ans['state']).encode())})"
                                           if "state" in ans else "") + "\n")
    return 0


def main(argv: Sequence[str], *, seams: Optional[Seams] = None, out: Optional[IO[str]] = None,
         err: Optional[IO[str]] = None) -> int:
    """``argv`` = the words after ``session``, its first one of :data:`VERBS`. Exit 0 or 1, every refusal by name."""
    out, err = out or sys.stdout, err or sys.stderr
    verb = argv[0]
    try:
        try:
            args = _parser(verb).parse_args(list(argv[1:]))
        except SystemExit as e:
            return 0 if e.code in (0, None) else 1
        session = _session(args.session)
        seams = seams or Seams()
        if verb == "adopt":
            if args.git is not None:
                seams.git = args.git
            if args.take is None and not (is_tty(seams.stdin or sys.stdin) and is_tty(out)):
                raise Refused("bad-argument", "--take head|wip|none is required unless both stdin and stdout are "
                                              "terminals (the prompt needs a person reading it)")
            if seams.git is None:
                from levain.launch import child_env
                seams.git = shutil.which("git", path=child_env().get("PATH")) or "git"
            return adopt(session, args.repo, args.take, seams, out, err)
        return store_verb(verb, session, seams, out, err)
    except Refused as e:
        err.write(f"levain session {verb}: refused {e.name}: {e.detail}\n")
        return 1
    except (ValueError, OSError) as e:      # canon's ContractError is a ValueError; an OS failure by its text
        err.write(f"levain session {verb}: refused os-error: {adopt_preview.escape(str(e).encode())}\n")
        return 1


__all__ = ["ANSWER_MAX", "EXPORT_CLASS", "HELPER_SLACK_S", "LARGE_COPIES", "PRESENCE_REGEXP", "PRIVATE_MARGIN",
           "PROMISOR_REGEXP", "REPO_GIT_TIMEOUT", "RESULTS", "STOP_GRACE_S", "STORE_SLACK_S", "STORE_VERBS",
           "STORE_VERB_CAS_CALLS", "Seams", "TAKES", "VERBS", "adopt", "ask_or_interrupt", "ask_take", "bundle_space",
           "call_helper", "check_base", "check_repo", "copy_private", "helper_timeout", "is_tty", "main", "open_dir",
           "open_published", "private_dir", "removal_line", "render", "render_report_only", "show_warnings", "store_verb",
           "verify_line", "warn_old_git"]
