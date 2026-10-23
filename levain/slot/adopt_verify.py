"""``quarantine_verify``: the ONE function that checks a session's export bundle (S1 session 5 DESIGN §6.1).

It runs inside the confined verifier (``levain-verify``, :mod:`levain.slot.verify_run`) as ``_levain_verify``, over the
two inputs root copied into the verify image: the published ``bundle`` and the session's BASE BUNDLE. The verifier never
sees the operator's repository. Every git here runs through :class:`importer.GitRunner` (``GIT_DIR`` pinned, no system or
global config, the C locale) with :data:`GIT_C` in front of each subcommand (DESIGN §6 setup).

Steps (§6.1): 1a the base store B from the base bundle's pack (no fsck: Levain's bytes, pinned by root to
``alloc.base.bundle_sha256``); 1 the header by :mod:`bundlefmt` and :func:`export_run.importer_bundle_check` (root's own
grammar, one site); 2 the quarantine store Q, the pack by ``index-pack --stdin --fsck-objects --no-rev-index``; 2a the
commit-count gate from Q's inventory (the tree reader of 2a is piece 5d-ii's, :data:`TREE_READER`); 3 the analysis repo P
(alternates B and Q); 4 the tips, the closure (connectivity first), the tip-in-base check, the EXACT SET; 5 ancestry
of each prerequisite. Built in piece 5d-i session 1.

Every byte fact in :class:`Verified` is a CLAIM the take re-derives (S3); only the verdict is the verifier's.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.slot import Refused, bundlefmt, export_run, fsck_ids, importer, special_paths

#: The ``-c`` set in front of every verifier git (DESIGN §6 setup, A15; threads pinned to 2, §6.3's RLIMIT_NPROC).
GIT_C = ("-c", "core.hooksPath=/dev/null", "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
         "-c", "pack.threads=2")
#: V30: new commits above this = ``preview-cap:commits`` (§6.3).
MAX_COMMITS = 10_000
#: The A4 header line, always the first line of ``preview.txt`` (§6.2 step 0).
PREVIEW_HEADER = ("PROPOSED by the session's importer: verified complete and exact, not verified faithful. Review it "
                  "as you would a patch from an untrusted contributor.")
#: Piece 5d-ii's 2a tree reader (the A10 reader: the below-ERROR ids and frame (a)'s depth bound). None until built:
#: 5d-i's verify therefore proves fsck + the exact set + ancestry, NOT the depth bound, and nothing takes from it.
TREE_READER: Optional[Callable[..., None]] = None
_PACK_SIG = b"PACK"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Verified:
    """§6.1's output. ``refs``: ``head`` / ``wip`` -> oid; ``q_idx``: Q's one idx, ``(name, sha256 hex)``."""

    result: str
    refs: Dict[str, str]
    prerequisites: Tuple[str, ...]
    pack_offset: int
    pack_sha256: str
    q_idx: Tuple[str, str]
    object_count: int

    def to_json(self) -> bytes:
        return canon.canonical_bytes({
            "v": 1, "result": self.result, "refs": dict(self.refs), "prerequisites": list(self.prerequisites),
            "pack_offset": self.pack_offset, "pack_sha256": self.pack_sha256,
            "q_idx": {"name": self.q_idx[0], "sha256": self.q_idx[1]}, "object_count": self.object_count})


class _Region:
    """A file from ``offset`` to its end, read in chunks as ``GitRunner`` stdin (its spool-slice duck type), hashed
    while read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sha = hashlib.sha256()
        self.read_bytes = 0

    def chunks(self, offset: int, size: int) -> Iterator[bytes]:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            left = size
            while left > 0:
                b = os.read(fd, min(_CHUNK, left))
                if not b:
                    break
                self.sha.update(b)
                self.read_bytes += len(b)
                left -= len(b)
                yield b
        finally:
            os.close(fd)


def read_header(path: Path) -> bundlefmt.BundleHeader:
    """The bundle's header by the shared grammar, from at most :data:`bundlefmt.HEADER_MAX` bytes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return read_header_at(fd)
    finally:
        os.close(fd)


def read_header_at(fd: int) -> bundlefmt.BundleHeader:
    """:func:`read_header` on an open fd, by ``pread`` from offset 0 (root's copy-in reads the published bundle and
    the base bundle through the fds it checked, 5d-i S2)."""
    head = os.pread(fd, bundlefmt.HEADER_MAX, 0)
    end = head.find(b"\n\n")
    if end < 0:
        raise Refused(bundlefmt.MISMATCH, "no blank line within the header cap")
    return bundlefmt.parse(head[:end + 2])


def pack_count(path: Path, offset: int, size: int) -> int:
    """The 12-byte pack header's object count; a count the region could not hold = ``bundle-header-mismatch``."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return pack_count_at(fd, offset, size)
    finally:
        os.close(fd)


def pack_count_at(fd: int, offset: int, size: int) -> int:
    """:func:`pack_count` on an open fd, by ``pread`` (root's copy-in, 5d-i S2)."""
    h = os.pread(fd, 12, offset)
    if len(h) != 12 or h[:4] != _PACK_SIG or int.from_bytes(h[4:8], "big") not in (2, 3):
        raise Refused(bundlefmt.MISMATCH, "the pack region does not start with a v2/v3 pack header")
    n = int.from_bytes(h[8:12], "big")
    if n > max(0, size - 12 - 20) // 2:      # every object costs at least 2 bytes (type/size + one zlib byte)
        raise Refused(bundlefmt.MISMATCH, f"a pack header count of {n} in {size} bytes")
    return n


def _runner(git: str, scratch: Path, name: str, log) -> importer.GitRunner:
    work = scratch / name
    for d in (work, work / "spool"):
        d.mkdir(mode=0o700)
    return importer.GitRunner(git, work, log)


def _g(r: importer.GitRunner, *args: str, **kw) -> Tuple[int, str, str]:
    return r.run(*GIT_C, *args, **kw)


def _must(r: importer.GitRunner, *args: str, **kw) -> str:
    rc, out, err = _g(r, *args, **kw)
    if rc != 0:
        last = (err.strip().splitlines() or [""])[-1]
        raise importer.Fatal(None, f"git {args[0]} rc {rc}: {last[:300]}")
    return out


def _init(r: importer.GitRunner, fmt: str) -> None:
    _must(r, "init", "--bare", "--template=", "-q", f"--object-format={fmt}")


#: Only ``error:`` / ``fatal:`` lines name the failure: a closed-list id on a ``warning:`` line is not the cause.
_FSCK_ID = re.compile(r"^(?:error|fatal): (?:object [0-9a-f]+: )?([A-Za-z]+):", re.MULTILINE)
_UNRESOLVED = re.compile(r"pack has \d+ unresolved delta")


def _index_pack_q(q: importer.GitRunner, region: _Region, offset: int, size: int, cap: int) -> None:
    """Step 2: the pack into Q. An unresolved delta = ``bundle-thin``; an fsck message = ``fsck:<id>`` (the closed
    list, else ``fsck:other``); any other failure has no verifier name (root answers ``verifier-failed:exit``)."""
    rc, _out, err = _g(q, "index-pack", "--stdin", "--no-rev-index", "--fsck-objects", f"--max-input-size={cap}",
                       "--threads=2", stdin=(region, offset, size))
    if rc == 0:
        return
    if _UNRESOLVED.search(err):
        raise Refused("bundle-thin", "the pack has unresolved deltas")
    for m in _FSCK_ID.finditer(err):
        ident = m.group(1)
        if ident in fsck_ids.closed_ids():
            raise Refused(f"fsck:{ident}", "index-pack --fsck-objects")
    if "fsck error" in err:
        raise Refused("fsck:other", (err.strip().splitlines() or [""])[-1][:300])
    raise importer.Fatal(None, f"index-pack rc {rc}: {(err.strip().splitlines() or [''])[-1][:300]}")


def _objects_dir(r: importer.GitRunner) -> Path:
    return r.work / "repo.git" / "objects"


def _one_pack(q: importer.GitRunner) -> Tuple[Path, Path]:
    pack_dir = _objects_dir(q) / "pack"
    names = sorted(os.listdir(pack_dir))
    packs = [n for n in names if n.endswith(".pack")]
    idxs = [n for n in names if n.endswith(".idx")]
    if len(packs) != 1 or len(idxs) != 1 or len(names) != 2 or packs[0][:-5] != idxs[0][:-4]:
        raise importer.Fatal(None, f"Q holds {names!r}, not exactly one .pack and its .idx")
    return pack_dir / packs[0], pack_dir / idxs[0]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    for chunk in importer._file_chunks(path):
        h.update(chunk)
    return h.hexdigest()


def _to_file(r: importer.GitRunner, path: Path, *args: str, stdin: Optional[Path] = None) -> None:
    fd = importer._new_file(path)
    try:
        _must(r, *args, stdout_fd=fd, stdin=stdin)
    finally:
        os.close(fd)


def _oid_lines(path: Path, hexlen: int, what: str) -> Iterator[str]:
    with importer._read_own(path) as fh:
        for line in importer._lines(fh, what):
            if len(line) != hexlen or not re.fullmatch(rb"[0-9a-f]+", line):
                raise importer.Fatal(None, f"{what}: a line that is not an oid")
            yield line.decode("ascii")


def _commit_gate(q: importer.GitRunner, scratch: Path) -> int:
    """2a's gate: Cq, the commits in Q, from Q's inventory (types only, no content read); above :data:`MAX_COMMITS` =
    ``preview-cap:commits`` before any commit is read."""
    inv = scratch / "q-types"
    _to_file(q, inv, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype)")
    with importer._read_own(inv) as fh:
        cq = sum(1 for line in importer._lines(fh, "the inventory") if line == b"commit")
    if cq > MAX_COMMITS:
        raise Refused("preview-cap:commits", f"{cq} commits in the bundle > {MAX_COMMITS}")
    return cq


def _required(p: importer.GitRunner, q: importer.GitRunner, scratch: Path, tips: Sequence[str], *, result: str,
              base_oid: str, hexlen: int) -> Tuple[Path, set, bool]:
    """Step 4's REQUIRED set, written sorted (``LC_ALL=C sort -T <scratch>``) to a file; returns it and the
    special oids and whether the walk met an absent object. DELTA: ``closure_P(tips) - closure_P(base)`` plus the
    adopt special set over the trees of that closure (:func:`special_paths.adopt_specials`, the importer's own function: one function, both callers);
    UNRELATED: ``closure_Q(tips)``. ``--missing=print`` lists an absent object as ``?<oid>`` instead of dying
    unnamed, so the merge below names it [judged by adopt-5di, 10-22, against §6.1 step 4's one-name-per-cause]. An
    absent PARENT commit, tree or blob prints ``?<oid>`` with rc 0 on both gits measured (``git --version``: Apple
    git 2.50.1 and git 2.43.0, Ubuntu 24.04; raw ``1022-20/linuxvm/s5d1_linux_raw.txt``). Only an absent TIP
    differs: 2.43.0 dies (rc 128, ``fatal: bad object``) where 2.50.1 prints ``?<tip>``. That case never reaches
    this walk: :func:`quarantine_verify` runs ``cat-file -t`` on every tip first and refuses
    ``bundle-tip-not-commit`` (row ``test_an_absent_tip_parent_or_tree_refuses_by_name_not_verifier_failed[tip-absent]``)
    [judged by linuxvm, 10-22, against the probe in that raw file]. Other git versions are not measured."""
    raw = scratch / "closure"
    r, extra = (p, ("--not", base_oid)) if result == "delta" else (q, ())
    _to_file(r, raw, "rev-list", "--objects", "--no-object-names", "--missing=print", *tips, *extra)
    present = scratch / "closure-present"
    req = scratch / "required-unsorted"
    absent = False
    with importer._read_own(raw) as fh, open(present, "xb") as pf, open(req, "xb") as rf:
        for line in importer._lines(fh, "rev-list"):
            oid = line[1:] if line[:1] == b"?" else line
            if len(oid) != hexlen or not re.fullmatch(rb"[0-9a-f]+", oid):
                raise importer.Fatal(None, "rev-list printed a line that is not an oid")
            rf.write(oid + b"\n")
            if line[:1] == b"?":
                absent = True
            else:
                pf.write(oid + b"\n")
    specials: set = set()
    if result == "delta":
        types = scratch / "closure-types"
        _to_file(p, types, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=present)
        trees = scratch / "closure-tree-oids"
        with importer._read_own(types) as fh, open(trees, "xb") as tf:
            for line in importer._lines(fh, "cat-file --batch-check"):
                oid, _, kind = line.partition(b" ")
                if kind == b"tree":
                    tf.write(oid + b"\n")
        payloads = scratch / "closure-trees"
        _to_file(p, payloads, "cat-file", "--batch", stdin=trees)
        try:
            specials = special_paths.adopt_specials(importer._tree_payloads(payloads, hexlen), hexlen)
        except ValueError as e:
            raise Refused("fsck:other", f"a tree Levain's reader refuses: {e}"[:300]) from None
        with open(req, "ab") as rf:
            for oid in sorted(specials):
                rf.write(oid.encode("ascii") + b"\n")
    out = scratch / "required"
    import subprocess
    with open(out, "xb") as of:
        rc = subprocess.run(["/usr/bin/sort", "-u", "-T", str(scratch), str(req)], stdout=of,
                            stderr=subprocess.DEVNULL, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).returncode
    if rc != 0:
        raise importer.Fatal(None, f"sort rc {rc}")
    return out, specials, absent


def _exact_set(sent: Path, required: Path, specials: set, hexlen: int) -> None:
    """``sent == required`` by a merge of two sorted lists, or one name per cause in this order: a required special
    missing from Q = ``bundle-special-unsent``; any other required oid missing = ``preview-graph-unresolved``; an oid in
    Q outside the required set = ``bundle-extra-objects`` (the count and the first 20)."""
    missing_special: List[str] = []
    missing: List[str] = []
    extra: List[str] = []
    n_extra = 0
    a = _oid_lines(sent, hexlen, "sent")
    b = _oid_lines(required, hexlen, "required")
    x, y = next(a, None), next(b, None)
    while x is not None or y is not None:
        if y is None or (x is not None and x < y):
            n_extra += 1
            if len(extra) < 20:
                extra.append(x)          # type: ignore[arg-type]
            x = next(a, None)
        elif x is None or y < x:
            (missing_special if y in specials else missing).append(y)
            y = next(b, None)
        else:
            x, y = next(a, None), next(b, None)
    if missing_special:
        raise Refused("bundle-special-unsent", f"{len(missing_special)} special blob(s), first {missing_special[0]}")
    if missing:
        raise Refused("preview-graph-unresolved", f"{len(missing)} required object(s) absent, first {missing[0]}")
    if n_extra:
        raise Refused("bundle-extra-objects", f"{n_extra} object(s) outside the required set: {' '.join(extra)}")


def quarantine_verify(bundle: Path, base_bundle: Path, *, session: str, base_oid: str, object_format: str,
                      result: str, scratch: Path, git: str, max_input_size: int, log=None) -> Verified:
    """§6.1 over the two inputs in the image; ``scratch``: an empty directory inside the image. Returns
    :class:`Verified` or raises :class:`Refused` by name (or :class:`importer.Fatal` with no name: a git failure
    the verifier does not name, which root answers ``verifier-failed:exit``)."""
    if result not in ("delta", "unrelated"):
        raise importer.Fatal(None, f"usage: --result {result!r}")
    hexlen = bundlefmt.OID_HEX[object_format]
    # 1a. the base store B
    b = _runner(git, scratch, "b", log)
    _init(b, object_format)
    try:
        bh = read_header(base_bundle)
        boid, _ref = bundlefmt.base_of(bh)
    except Refused as e:
        raise Refused("base-bundle-mismatch", f"{e.name}: {e.detail}") from None
    if bh.object_format != object_format or boid != base_oid:
        raise Refused("base-bundle-mismatch", "the base bundle's HEAD is not alloc.base's oid")
    bsize = os.stat(base_bundle, follow_symlinks=False).st_size
    _must(b, "index-pack", "--stdin", "--no-rev-index", "--threads=2", f"--max-input-size={bsize}",
          stdin=(_Region(base_bundle), bh.size, bsize - bh.size))
    rc, out, _ = _g(b, "cat-file", "-t", base_oid)
    if rc != 0 or out.strip() != "commit":
        raise Refused("base-bundle-mismatch", "the base commit is not in B after the base pack")
    # 1. the header
    h = read_header(bundle)
    export_run.importer_bundle_check(session, object_format)(h)   # refs non-empty and only head/wip: tips >= 1
    if bool(h.prerequisites) != (result == "delta"):
        raise Refused("export-result-mismatch", f"result {result} with {len(h.prerequisites)} prerequisite(s)")
    prefix = f"refs/levain/{session}/".encode()
    refs = {n[len(prefix):].decode("ascii"): o for o, n in h.refs}
    size = os.stat(bundle, follow_symlinks=False).st_size
    count = pack_count(bundle, h.size, size - h.size)
    # 2. Q
    q = _runner(git, scratch, "q", log)
    _init(q, object_format)
    region = _Region(bundle)
    _index_pack_q(q, region, h.size, size - h.size, max_input_size)
    if region.read_bytes != size - h.size:
        raise importer.Fatal(None, "index-pack stopped reading before the pack region's end")
    pack_sha = region.sha.hexdigest()
    _pack, idx = _one_pack(q)
    if os.path.lexists(_objects_dir(q) / "info" / "alternates"):
        raise importer.Fatal(None, "Q has an alternates file")
    # 2a. the gate (the reader is 5d-ii's)
    _commit_gate(q, scratch)
    if TREE_READER is not None:
        TREE_READER(q=q, b=b, scratch=scratch, hexlen=hexlen)
    # 3. P
    p = _runner(git, scratch, "p", log)
    _init(p, object_format)
    alt = _objects_dir(p) / "info"
    alt.mkdir(exist_ok=True)
    (alt / "alternates").write_text(f"{_objects_dir(b)}\n{_objects_dir(q)}\n")
    # 4. tips, connectivity, tip-in-base, the exact set. Each tip is a commit in Q BEFORE any git takes it as one (an
    # absent tip used to die unnamed in update-ref); the closure walk (``--missing=print``) runs BEFORE merge-base, and
    # merge-base runs only over a walk that met nothing absent (an absent parent used to die unnamed in merge-base).
    tips = [refs[k] for k in ("head", "wip") if k in refs]
    for t in tips:
        rc, out, _ = _g(q, "cat-file", "-t", t)
        if rc != 0 or out.strip() != "commit":
            raise Refused("bundle-tip-not-commit", t)
    for name, oid in refs.items():
        _must(p, "update-ref", f"refs/levain-preview/{name}", oid)
    sent = scratch / "sent"
    _to_file(q, sent, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    required, specials, absent = _required(p, q, scratch, tips, result=result, base_oid=base_oid, hexlen=hexlen)
    if not absent:                      # an absent object: the exact set below names it (graph-unresolved / unsent)
        for t in tips:
            rc, _o, err = _g(p, "merge-base", "--is-ancestor", t, base_oid)
            if rc == 0:
                raise Refused("bundle-tip-in-base", t)
            if rc != 1:                 # the importer's find_tips rule: only 1 means "not reachable from base"
                raise importer.Fatal(None, f"git merge-base rc {rc}: {(err.strip().splitlines() or [''])[-1][:300]}")
    _exact_set(sent, required, specials, hexlen)
    if absent:                          # unreachable by the merge (an absent oid is never in Q): fail closed, unnamed
        raise importer.Fatal(None, "the closure walk met an absent object the exact set did not name")
    # 5. ancestry
    for pre in h.prerequisites:
        rc, _o, _e = _g(p, "merge-base", "--is-ancestor", pre, base_oid)
        if rc != 0:
            raise Refused("base-mismatch", pre)
    return Verified(result, refs, h.prerequisites, h.size, pack_sha, (idx.name, _sha256_file(idx)), count)


# --- the verifier's result line, as root reads it (export_run.classify(..., role=VERIFIER, parse=...)) -------------

VERIFY_LINE_KEYS = frozenset({"v", "enospc", "refused", "result", "detail"})


def parse_verifier_line(raw: Optional[bytes]) -> export_run.Line:
    """The verifier's one result line (:func:`verify_run.result_line`): canonical JSON within
    :data:`importer.RESULT_CAP`, byte-equal to its own canonical JSON (``canon.canonical_bytes``: no whitespace, key
    order fixed), exactly :data:`VERIFY_LINE_KEYS`, ``v`` the integer 1 (never ``true``), ``enospc`` a strict
    boolean, ``refused`` a refusal name or null, ``result`` ``delta`` / ``unrelated`` or null. The verifier has no terminal frame: its
    ``output-missing`` is root's own check of ``out/``, so the line's ``terminal`` is always True."""
    if raw is None:
        raise export_run.LineBad("result-malformed", "no result line")
    if len(raw) > importer.RESULT_CAP:
        raise export_run.LineBad("result-oversize", f"{len(raw)} bytes")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise export_run.LineBad("result-malformed", "not one newline-terminated line")
    try:
        obj = canon.parse(raw[:-1])
        if not isinstance(obj, dict) or canon.canonical_bytes(obj) + b"\n" != raw:
            raise ValueError("not canonical JSON")
    except (ContractError, ValueError, TypeError) as e:
        raise export_run.LineBad("result-malformed", str(e)[:200]) from None
    if set(obj) != VERIFY_LINE_KEYS or type(obj["v"]) is not int or obj["v"] != 1:
        raise export_run.LineBad("result-malformed", "keys")
    if type(obj["enospc"]) is not bool or not isinstance(obj["detail"], str):
        raise export_run.LineBad("result-malformed", "enospc / detail types")
    ref = obj["refused"]
    if ref is not None and not (isinstance(ref, str) and export_run._NAME.fullmatch(ref)):
        raise export_run.LineBad("result-malformed", "refused")
    if obj["result"] not in (None, "delta", "unrelated"):
        raise export_run.LineBad("result-malformed", "result")
    return export_run.Line(True, obj["enospc"], ref, obj["result"], (0, 0))


__all__ = ["GIT_C", "MAX_COMMITS", "PREVIEW_HEADER", "TREE_READER", "VERIFY_LINE_KEYS", "Verified",
           "pack_count", "pack_count_at", "parse_verifier_line", "quarantine_verify", "read_header", "read_header_at"]
