"""The operator's side of adopt: ``levain session adopt|discard|restore <session>`` (S1 session 5 DESIGN §1 steps
13-14, §4.6, §4.8, §6 setup, §6.2; CONTRACT §10.3), routed from :func:`levain.slot.launch.main`.

``adopt <session> --repo PATH --take head|wip|none``, as the operator:

1. ``status <session>`` through the runtime helper: the alloc's ``base`` (none = ``base-unrecorded``).
2. :func:`check_repo`, BEFORE adopt-open, so a refusal costs no sealed run: ``repo-not-a-root`` (``--repo`` is not
   the top of a work tree that holds its own ``.git`` directory: a subdirectory, a linked worktree or a submodule
   checkout, whose ``.git`` is a file), ``object-format-mismatch`` (``extensions.objectFormat`` against the base's;
   ``extensions.compatObjectFormat`` set refuses too), ``repo-partial`` (``extensions.partialClone``, a
   ``remote.<name>.promisor`` that is not false, or a ``remote.<name>.partialCloneFilter``), ``repo-shallow``, and
   ``base-missing`` (``cat-file -e <base_oid>``).
3. ``adopt-open <session>`` ("waiting on a running import" is printed first: root's adopt-lock wait is bounded,
   §4.7); its answer checked against the alloc's base.
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
6. ``--take head|wip``: :func:`adopt_take.take` over the private copies; ``none`` takes nothing.

His git runs only on trusted input, each spawn through :func:`_git` (``-c core.hooksPath=/dev/null``,
:func:`adopt_take.git_env`, ``env=`` given): ``config --local --get-regexp`` (the partial-clone and object-format
read), ``rev-parse --is-shallow-repository``, ``cat-file -e <base_oid>``; the take adds ``cat-file -e`` per
prerequisite, ``rev-parse --git-path objects`` and ``update-ref``. A git that cannot start or outlives
:data:`REPO_GIT_TIMEOUT` = ``repo-unreadable:<errno name>`` / ``repo-unreadable:timeout``; one that answers the
config read or ``rev-parse`` with an rc it has no meaning for = ``repo-unreadable:<rc>``.

``discard <session>`` / ``restore <session>``: the root verb of the same name, its answer printed (§4.6).
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import os
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
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0", "")
#: The one config read: the keys :func:`check_repo` decides on (git prints section and key lowercased).
CONFIG_REGEXP = (r"^(extensions\.(partialclone|objectformat|compatobjectformat)"
                 r"|remote\..*\.(promisor|partialclonefilter))$")

Log = Optional[Callable[[str], None]]


def _note(log: Log, line: str) -> None:
    if log is not None:
        log(line)


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, "EIO")


# --- the runtime helper ---------------------------------------------------------------------------------------


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
    helper_timeout_s: float = 2 * 1800.0 + 600.0    # V7's lock wait + V7's wall + V30's slack [judged]
    log: Log = None
    spawn: Callable[..., "subprocess.CompletedProcess[bytes]"] = field(default=subprocess.run)

    def __post_init__(self) -> None:
        from levain.slot import layout

        host = "darwin" if sys.platform == "darwin" else "linux"
        if self.helper is None:
            self.helper = ("sudo", "-n", str(layout.runtime_helper_path(host)))   # type: ignore[arg-type]
        if self.store is None:
            self.store = layout.STORE[host]                                         # type: ignore[index]


def call_helper(seams: Seams, *args: str, err: IO[str]) -> Dict[str, object]:
    """One runtime-helper verb, stdin empty; its stderr shown escaped (root's detail may quote a session-made name),
    its stdout one canonical line (:func:`supervisor.parse_helper_line`)."""
    from levain.launch import child_env
    from levain.slot.supervisor import parse_helper_line

    argv = [*seams.helper, *args]                                                  # type: ignore[misc]
    _note(seams.log, "helper " + " ".join(args))
    try:
        p = seams.spawn(argv, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=seams.helper_timeout_s)
    except subprocess.TimeoutExpired:
        raise Refused("helper-timeout", args[0]) from None
    except OSError as e:
        raise Refused("helper-unrunnable", f"{args[0]}: {e.strerror or e}") from None
    for line in (p.stderr or b"").splitlines()[-5:]:
        if line.strip():
            err.write("  " + adopt_preview.escape(line.strip()) + "\n")
    return parse_helper_line(p.stdout or b"", args[0])


# --- the --repo checks (§4.8, A17) -----------------------------------------------------------------------------


def _git(seams: Seams, repo: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    argv = [seams.git or "git", "-c", "core.hooksPath=/dev/null", *args]
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
    """§4.8's checks, in this order, before adopt-open. Returns ``repo`` made absolute."""
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
    r = _git(seams, repo, "config", "--local", "--get-regexp", CONFIG_REGEXP)
    if r.returncode not in (0, 1):
        # His git cannot open the repository (git 2.54 without Rust refuses extensions.compatObjectFormat here:
        # s4a_run.txt row R5b): the take could not run either.
        raise Refused(f"repo-unreadable:{r.returncode}", f"git config: {_stderr(r)}")
    fmt = "sha1"
    for line in (r.stdout or b"").decode("utf-8", "surrogateescape").splitlines():
        key, _, value = line.partition(" ")
        k, v = key.lower(), value.strip().lower()
        if k == "extensions.objectformat":
            fmt = v
        elif k == "extensions.compatobjectformat":
            raise Refused("object-format-mismatch", f"extensions.compatObjectFormat = {adopt_preview.escape(v.encode())}"
                                                    ": the take writes no object map")
        elif k == "extensions.partialclone":
            raise Refused("repo-partial", "extensions.partialClone is set")
        elif k.endswith(".partialclonefilter"):
            raise Refused("repo-partial", f"{adopt_preview.escape(key.encode())} is set")
        elif k.endswith(".promisor") and v not in _FALSE:
            raise Refused("repo-partial", f"{adopt_preview.escape(key.encode())} = {adopt_preview.escape(v.encode())}")
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
    finally:
        os.close(out)
    if sha.hexdigest() != want:
        raise _changed(f"{dst.name}: its sha256 is not the digest root answered")
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
    return out


# --- the verbs --------------------------------------------------------------------------------------------------


def _parser(verb: str) -> argparse.ArgumentParser:
    from levain.slot.launch import _Parser

    p = _Parser(prog=f"levain session {verb}")
    p.add_argument("session")
    if verb == "adopt":
        p.add_argument("--repo", required=True, type=Path, metavar="PATH",
                       help="Your repository: the top of its work tree, holding the session's base commit.")
        p.add_argument("--take", choices=TAKES, required=True,
                       help="head or wip: create refs/heads/levain/<session>[-wip] from the export; none: preview "
                            "only.")
        p.add_argument("--git", default=None, metavar="GIT", help=argparse.SUPPRESS)
    return p


def _session(text: str) -> str:
    if not SESSION_RE.fullmatch(text):
        raise Refused("bad-argument", f"a session id s-<16 hex>, got {adopt_preview.escape(text.encode())}")
    return text


def adopt(session: str, repo: Path, which: str, seams: Seams, out: IO[str], err: IO[str]) -> int:
    rec = call_helper(seams, "status", session, err=err).get("records")
    if not isinstance(rec, list) or len(rec) != 1 or not isinstance(rec[0], dict):
        raise Refused("adopt-answer-malformed", "status: records")
    base = check_base(rec[0].get("base"))
    repo = check_repo(seams, repo, base)
    out.write("waiting on a running import (if one holds the adopt lock)...\n")
    out.flush()
    ans = call_helper(seams, "adopt-open", session, err=err)
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
    if result == "adopt-nothing-new":
        out.write(f"session {session}: nothing new since its base; nothing to take.\n")
        return 0
    if which != "none" and which not in refs:
        raise Refused("bad-argument", f"--take {which}: the export carries no {which} ref")
    fmt = str(base["object_format"])
    from levain.slot.importer import REPORT_CAP
    from levain.slot.runtime_helper import V7_BYTES, VERIFIED_CAP, idx_bound
    from levain.slot.verify_caps import PREVIEW_OUTPUT_CAP

    priv = private_dir(seams)
    bundle_copy = priv / "bundle"
    idx_copy = priv / "q.idx"
    try:
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
        v = call_helper(seams, "adopt-verify", session, "--bundle-digest", "sha256:" + _digest(ans, "bundle_digest"),
                        err=err)
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
        if which == "none":
            out.write("nothing taken (--take none); the export stays retained.\n")
            return 0
        t = adopt_take.take(git=seams.git or "git", repo=repo, bundle=bundle_copy, idx=idx_copy, claims=claims,
                            session=session, object_format=fmt, which=which, log=seams.log)
        out.write(f"took {which}: {t.ref} -> {t.oid}\n")
        return 0
    finally:
        for p in (bundle_copy, idx_copy):          # the large copies; the preview, report and claims stay readable
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


__all__ = ["CONFIG_REGEXP", "EXPORT_CLASS", "PRIVATE_MARGIN", "REPO_GIT_TIMEOUT", "RESULTS", "STORE_VERBS", "Seams",
           "TAKES", "VERBS", "adopt", "bundle_space", "call_helper", "check_base", "check_repo", "copy_private",
           "main", "open_dir", "open_published", "private_dir", "render", "store_verb"]
