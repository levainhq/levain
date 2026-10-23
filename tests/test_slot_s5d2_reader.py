"""S1 session 5 piece 5d-ii session 1: the A10 tree reader (DESIGN §6.1 step 2a, :mod:`levain.slot.fsck_reader`).

The rows come from the session's RUN (lane adopt-5dii, ``1022-20/adopt_5dii/s1_run.txt``, expected verdicts in that
dir's ``PLAN.md``); two of them are pinned here: the per-kind depth boundary through ``quarantine_verify`` and each
reader id. Trees are written as loose objects by the rig, so a shape git would never make (a zero-padded mode, a
4097-byte name) can exist; every row runs on the host git.
"""
import hashlib
import re
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused, fsck_ids, importer, walkframe
from levain.slot import adopt_verify as AV, bundlefmt as BF, fsck_reader as FR, verify_caps as VC

GIT = shutil.which("git")
S = "s-" + "d2" * 8
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C"}
needs_git = pytest.mark.skipif(GIT is None, reason="needs a git")


def g(*a, cwd=None, stdin=None) -> bytes:
    return subprocess.run([GIT, *a], cwd=cwd, env=ENV, check=True, capture_output=True, input=stdin).stdout


class Repo:
    """A base commit (``README``) and its bundle; new objects written loose by the rig."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.path = tmp / "repo"
        g("init", "-q", str(self.path))
        self.made: list = []
        self.readme = self.put(b"blob", b"base\n")
        self.base_tree = self.tree([(b"100644", b"README", self.readme)])
        self.base = self.commit(self.base_tree, [])
        g("update-ref", "refs/heads/main", self.base, cwd=self.path)
        g("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.path)
        self.base_bundle = tmp / "base.bundle"
        g("bundle", "create", "-q", str(self.base_bundle), "HEAD", "main", cwd=self.path)
        self.made.clear()

    def put(self, kind: bytes, body: bytes) -> str:
        data = b"%s %d\0" % (kind, len(body)) + body
        h = hashlib.sha1(data).hexdigest()
        p = self.path / ".git" / "objects" / h[:2] / h[2:]
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(zlib.compress(data))
        self.made.append(h)
        return h

    def tree(self, entries) -> str:
        def key(e):
            return e[1] + (b"/" if int(e[0], 8) & 0o170000 == 0o040000 else b"")
        return self.put(b"tree", b"".join(m + b" " + n + b"\0" + bytes.fromhex(o) for m, n, o in sorted(entries, key=key)))

    def commit(self, tree: str, parents) -> str:
        return self.put(b"commit", b"tree %s\n" % tree.encode() + b"".join(b"parent %s\n" % p.encode() for p in parents)
                        + b"author a <a@a> 0 +0000\ncommitter a <a@a> 0 +0000\n\nm\n")

    def export(self, root: str) -> Path:
        c = self.commit(root, [self.base])
        oids = [o for o in dict.fromkeys(self.made) if o != "0" * 40]
        pk = g("pack-objects", "-q", "--stdout", cwd=self.path, stdin=b"".join(o.encode() + b"\n" for o in oids))
        p = self.tmp / "export.bundle"
        p.write_bytes(BF.write_header("sha1", [(c, f"refs/levain/{S}/head".encode())], [self.base]) + pk)
        return p

    def verify(self, bundle: Path):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        logp = self.tmp / "git.log"
        with open(logp, "wb") as log:
            try:
                AV.quarantine_verify(bundle, self.base_bundle, session=S, base_oid=self.base, object_format="sha1",
                                     result="delta", scratch=scratch, git=GIT, max_input_size=1 << 30, log=log)
                got = "Verified"
            except Refused as r:
                got = r.name
        text = logp.read_bytes()
        stats = [canon.parse(ln[len(b"reader: "):]) for ln in text.splitlines() if ln.startswith(b"reader: ")]
        return got, (stats[0] if stats else None), text


def chain(r: Repo, end: int, kind: str) -> str:
    """A git path of ``end - offset`` components: ``end - offset - 1`` directories ``d`` above one leaf."""
    leaf = {"blob": lambda: (b"100644", b"f", r.put(b"blob", b"leaf\n")),
            "symlink": lambda: (b"120000", b"l", r.put(b"blob", b"target")),
            "gitlink": lambda: (b"160000", b"g", "ab" * 20),
            "empty": lambda: (b"40000", b"e", r.tree([]))}[kind]()
    t = r.tree([leaf])
    for _ in range(end - walkframe.end_level_offset() - 2):
        t = r.tree([(b"40000", b"d", t)])
    return r.tree([(b"40000", b"d", t)])


@needs_git
@pytest.mark.parametrize("kind", ["blob", "symlink", "gitlink", "empty"])
@pytest.mark.parametrize("end", [walkframe.MAX_DEPTH, walkframe.MAX_DEPTH + 1])
def test_every_entry_kind_passes_at_the_depth_bound_and_refuses_one_past_it(tmp_path, kind, end):
    # §9 per-kind rows (r10 S13-2) and the stack-against-height unit row (S13-3): every kind PASSES at END level
    # MAX_DEPTH and is ``preview-cap:tree-depth`` one level past it, from the reader, before any rev-list; the stack's
    # END value (edges + offset) never exceeds the height's: a non-empty leaf stops by the HEIGHT, an empty-tree leaf by
    # the stack's EARLY EXIT at the same END level.
    r = Repo(tmp_path)
    got, st, log = r.verify(r.export(chain(r, end, kind)))
    off = walkframe.end_level_offset()
    stack_end = st["stop_end"] if st["stop"] == "stack" else st["max_frames"] - 1 + off
    if end == walkframe.MAX_DEPTH:
        assert got == "Verified" and st["stop"] == ""
        assert st["max_height"] + off == end
        assert stack_end == (end if kind == "empty" else end - 1)
    else:
        assert got == "preview-cap:tree-depth" and b"rev-list" not in log
        assert (st["stop"], st["stop_end"]) == (("stack", end) if kind == "empty" else ("height", end))
    assert st["reparses"] == 0


READER_ROWS = {
    "nullSha1": lambda r: [(b"100644", b"z", "0" * 40)],
    "zeroPaddedFilemode": lambda r: [(b"0100644", b"z", r.put(b"blob", b"z\n"))],
    "fullPathname": lambda r: [(b"100644", b"a/b", r.put(b"blob", b"z\n"))],
    "badFilemode": lambda r: [(b"100664", b"z", r.put(b"blob", b"z\n"))],
    "gitattributesSymlink": lambda r: [(b"120000", b".gitattributes", r.put(b"blob", b"t"))],
    "largePathname": lambda r: [(b"100644", b"n" * (FR.LARGE_NAME + 1), r.put(b"blob", b"z\n"))],
    "hasDotgit": lambda r: [(b"40000", b".git", r.tree([(b"100644", b"f", r.put(b"blob", b"z\n"))]))],
    "hasDotgit-hfs": lambda r: [(b"40000", ".g‌it".encode(), r.tree([(b"100644", b"f", r.put(b"blob", b"z\n"))]))],
    "hasDotgit-ntfs": lambda r: [(b"40000", b"git~1", r.tree([(b"100644", b"f", r.put(b"blob", b"z\n"))]))],
    "hasDotgit-backslash": lambda r: [(b"40000", b"x\\GIT~1 ", r.tree([(b"100644", b"f", r.put(b"blob", b"z\n"))]))],
    "hasDotdot": lambda r: [(b"40000", b"..", r.tree([(b"100644", b"f", r.put(b"blob", b"z\n"))]))],
}


@needs_git
@pytest.mark.parametrize("case", sorted(READER_ROWS))
def test_each_reader_id_refuses_by_its_name_from_verify_and_from_the_reader_alone(tmp_path, case):
    # §9: each id of the reader's derived list in a NEW tree = ``fsck:<id>``. Through quarantine_verify index-pack
    # names the WARN ids first (``nullSha1`` was ``fsck:other`` until the id regex took digits: run 1, red); over a Q
    # made WITHOUT ``--fsck-objects`` the reader names every one itself.
    want = "fsck:" + case.split("-")[0]
    r = Repo(tmp_path)
    bun = r.export(r.tree(READER_ROWS[case](r) + [(b"100644", b"README", r.readme)]))
    if case != "hasDotgit-backslash":       # a backslash name: index-pack's own check differs per git; reader only
        assert r.verify(bun)[0] == want
    q, b, inv = direct(tmp_path, bun, r.base_bundle)
    with pytest.raises(Refused) as e:
        FR.read_trees(q, b, inv, 40)
    assert e.value.name == want


def test_the_reader_checks_every_derived_id_and_git_ids_read_with_their_digits():
    assert set(fsck_ids.reader_ids()) <= FR.CHECKED_IDS
    # every name the reader raises rides the wire verbatim (export_run.VERIFIER_NAMES), never as result-malformed
    from levain.slot import export_run as ER
    src = Path(FR.__file__).read_text()
    raised = set(re.findall(r'Refused\("([a-z:-]+)"', src)) | {FR.DEPTH, FR.UNRESOLVED}
    raised |= {f"fsck:{i}" for i in fsck_ids.reader_ids()}
    assert raised <= ER.VERIFIER_NAMES and "preview-cap:tree-depth" in raised
    for i in fsck_ids.closed_ids():
        assert re.fullmatch(fsck_ids.ID_PATTERN, i)
    line = "error in tree " + "ab" * 20 + ": nullSha1: contains entries pointing to null sha1"
    assert importer._FSCK_LINE.search(line).group(1) == "nullSha1"
    assert AV._FSCK_ID.search("error: object " + "ab" * 20 + ": nullSha1: x").group(1) == "nullSha1"


def test_the_image_budget_counts_every_scratch_inventory():
    # verify_caps.INVENTORY_LINE_EXTRA must name every oid inventory adopt_verify writes into scratch/ (the required
    # list and its sort temps have §6.3's own term; the closure trees have TREE_SCRATCH_CAP; b/q/p are the stores).
    src = Path(AV.__file__).read_text()
    names = set(re.findall(r'scratch / "([a-z-]+)"', src))
    assert names - {"required", "closure-trees"} == set(VC.INVENTORY_LINE_EXTRA)
    assert VC.inventory_bytes(2, 40) == 2 * sum(40 + x for x in VC.INVENTORY_LINE_EXTRA.values())


# --- 5d-ii S1 L3 r1 fixes (lane adopt5dii-fix1, findings 1022-21/adopt5dii/L3_r1.txt) ------------------------------

def direct(tmp: Path, bun: Path, base_bundle: Path):
    """Q and B indexed WITHOUT ``--fsck-objects`` (the reader alone judges), and Q's inventory written by the rig."""
    d = tmp / "direct"
    d.mkdir()
    q, b = AV._runner(GIT, d, "q", None), AV._runner(GIT, d, "b", None)
    for runner, src in ((q, bun), (b, base_bundle)):
        AV._init(runner, "sha1")
        h = AV.read_header(src)
        size = src.stat().st_size
        AV._must(runner, "index-pack", "--stdin", "--no-rev-index", stdin=(AV._Region(src), h.size, size - h.size))
    inv = d / "inventory"
    inv.write_bytes(g("--git-dir", str(q.work / "repo.git"), "cat-file", "--batch-all-objects",
                      "--batch-check=%(objectname) %(objecttype)"))
    return q, b, inv


def tree_cost(r: Repo, oid: str) -> int:
    """What the reader counts for one tree: cat-file's whole response, ``<oid> tree <size>\\n<payload>\\n``."""
    size = int(g("cat-file", "-s", oid, cwd=r.path))
    return len(f"{oid} tree {size}\n") + size + 1


def asks(monkeypatch) -> list:
    seen: list = []
    real = FR._Store._ask

    def spy(self, cmd, oid):
        seen.append((cmd, oid))
        return real(self, cmd, oid)
    monkeypatch.setattr(FR._Store, "_ask", spy)
    return seen


def files(r: Repo, n: int, tag: bytes):
    return [(b"100644", b"%s%05d" % (tag, i), r.put(b"blob", b"%s %d\n" % (tag, i))) for i in range(n)]


@needs_git
@pytest.mark.parametrize("over", [0, 1])
def test_one_tree_at_the_output_cap_passes_and_one_byte_over_refuses_before_its_contents(tmp_path, monkeypatch, over):
    # L3 r1 MED (codex + complement): the reader read a tree's whole expanded payload with no cap. Now the probe's size
    # is checked against verify_caps.EXPANDED_OUTPUT_CAP BEFORE ``contents``: at the cap it passes, one byte under its
    # cost it is ``preview-cap:output`` and the tree's contents are never requested.
    r = Repo(tmp_path)
    root = r.tree(files(r, 200, b"f"))
    bun = r.export(root)
    q, b, inv = direct(tmp_path, bun, r.base_bundle)
    monkeypatch.setattr(VC, "EXPANDED_OUTPUT_CAP", tree_cost(r, root) - over)
    seen = asks(monkeypatch)
    if over:
        with pytest.raises(Refused) as e:
            FR.read_trees(q, b, inv, 40)
        assert e.value.name == "preview-cap:output"
        assert ("info", root) in seen and ("contents", root) not in seen
    else:
        assert FR.read_trees(q, b, inv, 40).uq == 1
        assert ("contents", root) in seen


@needs_git
@pytest.mark.parametrize("over", [0, 1])
def test_the_reader_output_cap_is_cumulative_over_every_tree_it_reads(tmp_path, monkeypatch, over):
    # The running total: three trees each well under the cap, together over it, refuse ``preview-cap:output`` at the
    # tree whose contents would pass it (never requested); at exactly their sum they pass.
    r = Repo(tmp_path)
    a, c = r.tree(files(r, 100, b"a")), r.tree(files(r, 100, b"c"))
    root = r.tree([(b"40000", b"a", a), (b"40000", b"c", c)])
    bun = r.export(root)
    q, b, inv = direct(tmp_path, bun, r.base_bundle)
    costs = {t: tree_cost(r, t) for t in (root, a, c)}
    total = sum(costs.values())
    assert max(costs.values()) < total - 1
    monkeypatch.setattr(VC, "EXPANDED_OUTPUT_CAP", total - over)
    seen = asks(monkeypatch)
    if over:
        with pytest.raises(Refused) as e:
            FR.read_trees(q, b, inv, 40)
        assert e.value.name == "preview-cap:output"
        read = [o for cmd, o in seen if cmd == "contents" and o in costs]
        assert len(read) == 2
        last = next(t for t in costs if t not in read)
        assert ("info", last) in seen
    else:
        assert FR.read_trees(q, b, inv, 40).uq == 3


@needs_git
def test_absent_oids_named_by_a_tiny_pack_refuse_by_the_inventory_share_not_by_space(tmp_path):
    # L3 r1 codex MED (the lane's own Q5, ruled FIX): a 2-object pack whose tree names many distinct ABSENT blobs grows
    # step 4's ``closure`` (``--missing=print``) far past its share of the image budget root sized from the packs'
    # object counts. Every inventory now goes through one bounded writer: ``preview-cap:output``, never space.
    r = Repo(tmp_path)
    fake = [(b"100644", b"x%05d" % i, hashlib.sha1(b"absent %d" % i).hexdigest()) for i in range(2000)]
    got, _st, _log = r.verify(r.export(r.tree(fake + [(b"100644", b"README", r.readme)])))
    assert got == "preview-cap:output"


@needs_git
def test_a_delta_of_many_symlinks_over_a_tiny_base_stays_within_every_share(tmp_path):
    # The bounded writer must never refuse a real bundle: symlink targets are adopt specials AND closure objects, and
    # appending each one again put ``required-unsorted`` past its share for 60 symlinks over a 3-object base (this
    # lane's build, before the specials were filtered against the closure).
    r = Repo(tmp_path)
    links = [(b"120000", b"l%03d" % i, r.put(b"blob", b"target-%d" % i)) for i in range(60)]
    assert r.verify(r.export(r.tree(links + [(b"100644", b"README", r.readme)])))[0] == "Verified"


def test_the_inventory_writer_refuses_past_its_share(tmp_path):
    # The one bounded writer by itself: its share is verify_caps.inventory_share for its file name; one byte past it
    # is ``preview-cap:output``; the shares sum to the image budget's inventory term.
    share = VC.inventory_share("sent", 3, 40)
    assert share == 3 * 41 and VC.inventory_bytes(3, 40) == sum(
        VC.inventory_share(n, 3, 40) for n in VC.INVENTORY_LINE_EXTRA)
    with AV._Inventory(tmp_path / "sent", 3, 40) as w:
        w.write(b"a" * share)
        with pytest.raises(Refused) as e:
            w.write(b"a")
    assert e.value.name == "preview-cap:output"


class FullLog:
    def write(self, b):
        raise OSError(28, "No space left on device")

    def flush(self):
        raise OSError(28, "No space left on device")


@needs_git
@pytest.mark.parametrize("deep", [False, True])
def test_the_stats_line_never_changes_the_verdict(tmp_path, deep):
    # L3 r1 complement LOW: the stats write in read_trees' ``finally`` raised ENOSPC over the verdict. A log that
    # raises leaves a pass a pass and a refusal its own name.
    r = Repo(tmp_path)
    root = chain(r, walkframe.MAX_DEPTH + 1, "blob") if deep else r.tree([(b"100644", b"README", r.readme)])
    q, b, inv = direct(tmp_path, r.export(root), r.base_bundle)
    if deep:
        with pytest.raises(Refused) as e:
            FR.read_trees(q, b, inv, 40, log=FullLog())
        assert e.value.name == "preview-cap:tree-depth"
    else:
        assert FR.read_trees(q, b, inv, 40, log=FullLog()).uq == 1


def test_a_cat_file_failure_carries_its_stderr_tail(tmp_path):
    # L3 r1 complement LOW: _Store discarded cat-file's stderr. A cat-file that dies names its last stderr line.
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho 'fatal: the boom line' >&2\nexit 128\n")
    fake.chmod(0o755)
    work = tmp_path / "w"
    work.mkdir()
    st = FR._Store(importer.GitRunner(str(fake), work), "Q", 40)
    try:
        with pytest.raises(importer.Fatal) as e:
            st.info("ab" * 20)
    finally:
        st.close()
    assert "the boom line" in e.value.detail


@needs_git
def test_a_commit_without_a_leading_tree_line_is_fsck_other(tmp_path):
    # L3 r1 complement LOW: it raised Fatal(None, ...) (root's generic verifier-failed); now ``fsck:other``.
    r = Repo(tmp_path)
    bad = r.put(b"commit", b"parent %s\nauthor a <a@a> 0 +0000\ncommitter a <a@a> 0 +0000\n\nm\n" % r.base.encode())
    pk = g("pack-objects", "-q", "--stdout", cwd=r.path, stdin=bad.encode() + b"\n")
    bun = tmp_path / "bad.bundle"
    bun.write_bytes(BF.write_header("sha1", [(bad, f"refs/levain/{S}/head".encode())], [r.base]) + pk)
    q, b, inv = direct(tmp_path, bun, r.base_bundle)
    with pytest.raises(Refused) as e:
        FR.read_trees(q, b, inv, 40)
    assert e.value.name == "fsck:other"


def test_fsck_ids_all_is_sorted():
    from levain.slot import fsck_ids as FI
    assert FI.__all__ == sorted(FI.__all__)
