"""``levain-verify``: the program that runs as ``_levain_verify`` inside the ``verifier`` wall (S1 session 5 DESIGN §6.0).

Root launches it with its cwd at the mount of ITS per-run image (``.verify/``), whose layout root made at seal-in:
``in/bundle`` and ``in/base.bundle`` (root's copies, hashed while copied), ``scratch/`` (empty), ``out/`` (empty) and
``log/``. It runs :func:`adopt_verify.quarantine_verify` and, on a pass, writes ``out/verified.json``, ``out/q.idx``
(Q's one idx, byte for byte) and ``out/preview.txt`` (§6.2, :func:`adopt_preview.write_preview` over the P it left),
then answers ONE result line on fd 4 (:func:`result_line`). Every exit answers exactly one line, written at one
place; a failed run leaves no file in ``out/``. ``enospc`` is SOURCE-SIDE, from the verifier's own readings on its
fatal path only (the importer's rule, :func:`importer.probe_enospc` over ``scratch/``).
"""
from __future__ import annotations

import errno
import os
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

from levain.contracts import canon
from levain.slot import Refused, adopt_preview, adopt_verify, bundlefmt, importer, write_all
from levain.slot.grammar import SESSION_RE

RESULT_FD = 4
OUTPUTS = ("verified.json", "q.idx", "preview.txt")
_DETAIL_MAX = 1000


def result_line(*, refused: Optional[str], detail: str, enospc: bool, result: Optional[str]) -> bytes:
    """Canonical JSON, newline-terminated, within :data:`importer.RESULT_CAP` (keys: ``adopt_verify.VERIFY_LINE_KEYS``)."""
    body = {"v": 1, "enospc": enospc, "refused": refused, "result": result,
            "detail": detail[:_DETAIL_MAX].encode("utf-8", "replace").decode("utf-8")}
    return canon.canonical_bytes(body) + b"\n"


_UNBUILDABLE = canon.canonical_bytes({"v": 1, "enospc": False, "refused": "result-unbuildable", "result": None,
                                      "detail": ""}) + b"\n"


def _parser() -> importer._Parser:
    ap = importer._Parser(prog="export-verifier", add_help=False)
    ap.add_argument("--git", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--object-format", required=True, choices=sorted(bundlefmt.OID_HEX))
    ap.add_argument("--base-oid", required=True)
    ap.add_argument("--result", required=True, choices=("delta", "unrelated"))
    ap.add_argument("--max-input-size", required=True, type=int)
    ap.add_argument("--git-log")
    return ap


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_new(src: Path, dst: Path) -> None:
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        for chunk in importer._file_chunks(src):
            write_all(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_outputs(work: Path, v: adopt_verify.Verified, q_idx: Path, *, git: str, base_oid: str, object_format: str,
                  log=None) -> None:
    """The preview first (it can still refuse, ``preview-cap:output``, before any output exists), then the two
    others."""
    out = work / "out"
    p = importer.GitRunner(git, work / "scratch" / "p", log)
    adopt_preview.write_preview(out / "preview.txt", p, v, base_oid=base_oid, hexlen=bundlefmt.OID_HEX[object_format])
    _write_new(out / "verified.json", v.to_json() + b"\n")
    _copy_new(q_idx, out / "q.idx")


def remove_outputs(work: Path) -> bool:
    ok = True
    for n in OUTPUTS:
        try:
            os.unlink(work / "out" / n)
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
    return ok


def run(argv: Sequence[str], *, result_fd: int, work: Optional[Path],
        startup_error: Optional[OSError] = None) -> int:
    out = {"rc": 1, "refused": None, "detail": "verifier aborted", "enospc": False, "result": None}
    log = None
    interrupted = False                 # set only by THIS run's BaseException, never by a caller's active exception
    try:
        try:
            if startup_error is not None:
                raise importer.Fatal(None, f"startup: {errno.errorcode.get(startup_error.errno or 0, '?')}",
                                     own_enospc=startup_error.errno in (errno.ENOSPC, errno.EDQUOT))
            assert work is not None
            try:
                a = _parser().parse_args(list(argv))
            except importer._Usage as u:
                raise importer.Fatal(None, f"usage: {u}") from None
            if not re.fullmatch(r"[0-9a-f]{%d}" % bundlefmt.OID_HEX[a.object_format], a.base_oid):
                raise importer.Fatal(None, "usage: --base-oid is not an oid of the object format")
            if not SESSION_RE.fullmatch(a.session):
                raise importer.Fatal(None, "usage: --session is not a session id")
            if a.git_log:
                log = open(a.git_log, "ab")
            v = adopt_verify.quarantine_verify(
                work / "in" / "bundle", work / "in" / "base.bundle", session=a.session, base_oid=a.base_oid,
                object_format=a.object_format, result=a.result, scratch=work / "scratch", git=a.git,
                max_input_size=a.max_input_size, log=log)
            write_outputs(work, v, work / "scratch" / "q" / "repo.git" / "objects" / "pack" / v.q_idx[0],
                          git=a.git, base_oid=a.base_oid, object_format=a.object_format, log=log)
            out.update(rc=0, refused=None, detail="", enospc=False, result=v.result)
        except Refused as r:
            out.update(rc=1, refused=r.name, detail=r.detail, result=None)
        except importer.Fatal as f:
            out.update(rc=1, refused=f.name, detail=f.detail, result=None,
                       enospc=f.own_enospc or _probe(work))
        except OSError as e:
            out.update(rc=1, refused=None, detail=f"OSError {errno.errorcode.get(e.errno or 0, '?')}", result=None,
                       enospc=e.errno in (errno.ENOSPC, errno.EDQUOT) or _probe(work))
        except Exception as e:  # noqa: BLE001 - every failure answers one line
            out.update(rc=1, refused=None, detail=f"{type(e).__name__}: {e}", result=None, enospc=_probe(work))
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
    except BaseException:
        interrupted = True
        raise
    finally:
        if interrupted:
            out.update(rc=1, refused=None, result=None, detail="verifier interrupted")
        if out["rc"] != 0 and work is not None and not _remove_quietly(work):
            out["detail"] = f"{out['detail']}; an output could not be removed"
        try:
            line = result_line(refused=out["refused"], detail=str(out["detail"]), enospc=bool(out["enospc"]),
                               result=out["result"] if out["rc"] == 0 else None)
            if len(line) > importer.RESULT_CAP:
                line = result_line(refused=out["refused"], detail="", enospc=bool(out["enospc"]),
                                   result=out["result"] if out["rc"] == 0 else None)
        except Exception:  # noqa: BLE001
            line, out["rc"] = _UNBUILDABLE, 1
        try:
            write_all(result_fd, line)          # THE one result-line write
        except OSError:
            out["rc"] = 1
            if work is not None:
                _remove_quietly(work)
    return int(out["rc"])


def _probe(work: Optional[Path]) -> bool:
    """The importer's fatal-path probe (``importer.probe_enospc``, which reads ``<work>/spool``) over ``scratch/``."""
    if work is None:
        return False
    try:
        return _scratch_probe(work)
    except Exception:  # noqa: BLE001
        return False


def _scratch_probe(work: Path) -> bool:
    try:
        if os.statvfs(work / "scratch").f_bavail == 0:
            return True
    except OSError:
        pass
    p = work / "scratch" / f".probe-{os.getpid()}"
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        return e.errno in (errno.ENOSPC, errno.EDQUOT)
    try:
        write_all(fd, b"\0" * 4096)
        os.fsync(fd)
        return False
    except OSError as e:
        return e.errno in (errno.ENOSPC, errno.EDQUOT)
    finally:
        os.close(fd)
        try:
            os.unlink(p)
        except OSError:
            pass


def _remove_quietly(work: Path) -> bool:
    try:
        return remove_outputs(work)
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    work: Optional[Path] = None
    err: Optional[OSError] = None
    try:
        work = Path(os.getcwd())
    except OSError as e:
        err = e
    return run(sys.argv[1:], result_fd=RESULT_FD, work=work, startup_error=err)


if __name__ == "__main__":
    sys.exit(main())
