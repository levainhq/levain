"""S1 session 5 piece 5d-ii session 2: §6.2 content, ``out/preview.txt`` (:mod:`levain.slot.adopt_preview`).

The rows come from the session's RUN (lane adopt5dii-s2, ``1022-21/adopt5dii/s2_run.txt``, the expected text in that
dir's ``s2_expected.md``, written before it ran), revised and extended by the S2 fix1 RUN (lane adopt5dii-s2fix1,
``s2f1_run.txt``, expected text ``s2f1_expected.md``; the L3 s2 r1 findings, rows F1-F7) and by the S2 fix2 RUN
(lane adopt5dii-s2fix2, ``1023-1/adopt5dii/s2f2_run.txt``, ``s2f2_expected.md``; the L3 s2 r2 findings, rows G1-G6).
Objects are written loose by the rig (any byte can sit in a commit or a name), the export bundle is
``bundlefmt.write_header`` + ``pack-objects``, and every row runs on the host git.
"""
import hashlib
import os
import shutil
import subprocess
import unicodedata
import zlib
from pathlib import Path
from typing import Optional

import pytest

from levain.slot import Refused, importer
from levain.slot import adopt_preview as AP, adopt_verify as AV, bundlefmt as BF, verify_caps as VC

GIT = shutil.which("git")
S = "s-" + "d3" * 8
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C"}
needs_git = pytest.mark.skipif(GIT is None, reason="needs a git")
HOSTILE = b"subj \x1b[31mred\x1b[0m \xe2\x80\xae evil \xc2\x9b c1 \x9b raw \x01\x7f\x1b[2J nul-adjacent \\ back\n"
HOSTILE_SHOWN = ("    ⁨subj \\u001b[31mred\\u001b[0m \\u202e evil \\u009b c1 \\x9b raw \\u0001\\u007f\\u001b[2J "
                 "nul-adjacent \\\\ back⁩")
#: The PATH_MAX warning's tail: the checkout prefix is not known to the verifier (S2 fix2, codex 4).
PREFIX_UNKNOWN = "on its own; your checkout's path (not known to the verifier) adds to it: a checkout may fail"


def w(s: str) -> str:
    """A field as the file shows it: isolated in FSI .. PDI."""
    return AP.FSI + s + AP.PDI


def g(*a, cwd=None, stdin=None) -> bytes:
    return subprocess.run([GIT, *a], cwd=cwd, env=ENV, check=True, capture_output=True, input=stdin).stdout


class Repo:
    """A base commit (``README``) and its bundle; every later object written loose by the rig."""

    def __init__(self, tmp: Path, base_parents: int = 0) -> None:
        self.tmp = tmp
        self.path = tmp / "repo"
        g("init", "-q", str(self.path))
        self.made: list = []
        self.readme = self.blob(b"base\n")
        self.first = self.commit(self.tree([(b"100644", b"README", self.blob(b"one\n"))]), [])
        parents = [self.first] if base_parents else []
        self.base_tree = self.tree([(b"100644", b"README", self.readme)])
        self.base = self.commit(self.base_tree, parents)
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

    def blob(self, data: bytes) -> str:
        return self.put(b"blob", data)

    def tree(self, entries) -> str:
        def key(e):
            return e[1] + (b"/" if int(e[0], 8) & 0o170000 == 0o040000 else b"")
        return self.put(b"tree", b"".join(m + b" " + n + b"\0" + bytes.fromhex(o) for m, n, o in sorted(entries, key=key)))

    def files(self, files) -> str:
        """A root tree from ``{path: (mode, oid)}`` plus the base ``README``."""
        def build(items):
            here, subs = [], {}
            for path, mo in items.items():
                head, sep, rest = path.partition(b"/")
                if sep:
                    subs.setdefault(head, {})[rest] = mo
                else:
                    here.append((mo[0], head, mo[1]))
            return self.tree(here + [(b"40000", d, build(sub)) for d, sub in subs.items()])
        return build({b"README": (b"100644", self.readme), **files})

    def commit(self, tree: str, parents, msg: bytes = b"m\n", author: bytes = b"a <a@a> 0 +0000") -> str:
        return self.put(b"commit", b"tree %s\n" % tree.encode() + b"".join(b"parent %s\n" % p.encode() for p in parents)
                        + b"author %s\ncommitter a <a@a> 0 +0000\n\n" % author + msg)

    def export(self, tip: str, prereqs) -> Path:
        oids = [o for o in dict.fromkeys(self.made) if o not in (self.readme, self.base, self.first, self.base_tree)]
        pk = g("pack-objects", "-q", "--stdout", cwd=self.path, stdin=b"".join(o.encode() + b"\n" for o in oids))
        p = self.tmp / "export.bundle"
        p.write_bytes(BF.write_header("sha1", [(tip, f"refs/levain/{S}/head".encode())], list(prereqs)) + pk)
        return p

    def delta(self, files, **kw) -> Path:
        return self.export(self.commit(self.files(files), [self.base], **kw), [self.base])

    def export_refs(self, refs, prereqs) -> Path:
        """An export bundle with ``refs`` (``[(oid, "head" | "wip")]``)."""
        oids = [o for o in dict.fromkeys(self.made) if o not in (self.readme, self.base, self.first, self.base_tree)]
        pk = g("pack-objects", "-q", "--stdout", cwd=self.path, stdin=b"".join(o.encode() + b"\n" for o in oids))
        p = self.tmp / "export.bundle"
        p.write_bytes(BF.write_header("sha1", [(o, f"refs/levain/{S}/{k}".encode()) for o, k in refs], list(prereqs))
                      + pk)
        return p

    def preview(self, bundle: Path, result: str = "delta", patch=None, monkeypatch=None, path_max=1024,
                detail=False) -> str:
        """quarantine_verify, then write_preview over its P; the text, or the refusal's name (and detail)."""
        scratch = self.tmp / "scratch"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        out = self.tmp / "preview.txt"
        if out.exists():
            out.unlink()
        v = AV.quarantine_verify(bundle, self.base_bundle, session=S, base_oid=self.base, object_format="sha1",
                                 result=result, scratch=scratch, git=GIT, max_input_size=1 << 30)
        with pytest.MonkeyPatch.context() as mp:
            for k, val in (patch or {}).items():
                mp.setattr(VC, k, val)
            try:
                AP.write_preview(out, importer.GitRunner(GIT, scratch / "p", None), v, base_oid=self.base,
                                 hexlen=40, path_max=path_max)
            except Refused as r:
                assert not out.exists()
                return (r.name, r.detail) if detail else r.name
        return out.read_text(encoding="utf-8")


def section(text: str, title: str, n: Optional[int] = None) -> list:
    """A section's first ``n`` lines, its header first; ``n=None``: the whole section, up to the next header."""
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"== {title} ("))
    if n is None:
        n = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("== ")), len(lines)) - i
    return lines[i:i + n]


@needs_git
def test_a_hostile_commit_is_escaped_and_no_hidden_character_reaches_the_file(tmp_path):
    # RUN row E1: terminal escapes, U+202E, C1 as UTF-8 and as a raw byte, the bytes beside a NUL, a backslash
    r = Repo(tmp_path)
    text = r.preview(r.delta({b"a.txt": (b"100644", r.blob(b"a\n"))}, msg=HOSTILE,
                             author=b"ev\x1b]0;t\x07il <e@x> 0 +0000"))
    lines = text.splitlines()
    assert lines[0] == AV.PREVIEW_HEADER
    assert "  author " + w("ev\\u001b]0;t\\u0007il <e@x> 0 +0000") in lines and HOSTILE_SHOWN in lines
    # the only format characters left are the isolates the preview emits, balanced on every line (S2 fix1 row E1)
    assert not [c for c in text if (ord(c) < 0x20 and c != "\n") or 0x7f <= ord(c) <= 0x9f
                or (unicodedata.category(c) == "Cf" and c not in (AP.FSI, AP.PDI))]
    assert not [ln for ln in lines if ln.count(AP.FSI) != ln.count(AP.PDI)]


@needs_git
def test_a_nul_in_a_commit_message_is_refused_by_its_own_name(tmp_path):
    # RUN row E1n / F7 (head 1022+21 Q6): index-pack --fsck-objects says nulInCommit, now in the closed list
    r = Repo(tmp_path)
    with pytest.raises(Refused) as e:
        r.preview(r.delta({}, msg=HOSTILE.replace(b"\x01\x7f", b"\x00")))
    assert e.value.name == "fsck:nulInCommit"


@needs_git
def test_a_long_path_is_capped_before_escaping_and_warned_past_path_max(tmp_path):
    # RUN row E2
    long = b"/".join([b"p" * 1023] * 5 + [b"ffff"])
    r = Repo(tmp_path)
    lines = r.preview(r.delta({long: (b"100644", r.blob(b"x\n"))})).splitlines()
    assert "A 100644 " + w(f"{long[:4096].decode()} [{len(long)} bytes, sha256 "
                           f"{hashlib.sha256(long).hexdigest()[:16]}]") in lines
    assert f"  warning: {len(long)} bytes reaches PATH_MAX (1024) {PREFIX_UNKNOWN}" in lines
    assert "  warning: a 1023-byte component is past NAME_MAX (255): a checkout may fail" in lines


@needs_git
def test_path_max_warns_at_equality_at_the_lower_of_macos_and_the_host_and_past_name_max(tmp_path, monkeypatch):
    # S2 fix1 row F6b (complement LOW 4, codex LOW): >= not >, min(1024, host) when the host says 4096, NAME_MAX 255
    p1024 = b"/".join([b"q" * 200] * 5) + b"/" + b"r" * 19
    p256 = b"d/" + b"x" * 256
    assert len(p1024) == 1024
    monkeypatch.setattr(AP, "_path_max", lambda: 4096)
    r = Repo(tmp_path)
    lines = r.preview(r.delta({p1024: (b"100644", r.blob(b"1\n")), p256: (b"100644", r.blob(b"2\n"))}),
                      path_max=None).splitlines()
    assert f"  warning: 1024 bytes reaches PATH_MAX (1024) {PREFIX_UNKNOWN}" in lines
    assert "  warning: a 256-byte component is past NAME_MAX (255): a checkout may fail" in lines


@needs_git
def test_a_section_keeps_its_first_entries_then_groups_the_rest(tmp_path):
    # RUN row E3
    r = Repo(tmp_path)
    n = VC.SECTION_LINES + 1
    text = r.preview(r.delta({b"many/f%03d" % i: (b"100644", r.blob(b"%d\n" % i)) for i in range(n)}))
    assert section(text, "changes base..head", n + 1) == [f"== changes base..head ({n}) =="] + \
        [f"A 100644 {w(f'many/f{k:03d}')}" for k in range(n - 1)] + \
        [w("many") + "/: 1 more not listed, 0 flagged listed above"]
    # the full listing names every path, uncut (head Q2)
    full = section(text, "every path the take writes", n + 1)
    assert full[0] == f"== every path the take writes ({n}) ==" and full[-1] == f"base..head A 100644 {w('many/f200')}"


@needs_git
def test_flagged_entries_are_never_folded_into_the_count(tmp_path):
    # S2 fix1 row F2 (complement MED 2): 201 benign links, then one pointing outside
    r = Repo(tmp_path)
    x = r.blob(b"x")
    files = {b"a/l%03d" % i: (b"120000", x) for i in range(VC.SECTION_LINES + 1)}
    files[b"zzz/up"] = (b"120000", r.blob(b"/etc"))
    lines = section(r.preview(r.delta(files)), "symlinks", VC.SECTION_LINES + 3)
    assert lines[0] == f"== symlinks ({VC.SECTION_LINES + 2}) =="
    assert lines[-2:] == [f"base..head A {w('zzz/up')} -> {w('/etc')}  [points outside the repository]",
                          w("a") + "/: 1 more not listed, 0 flagged listed above"]


@needs_git
def test_commits_past_the_section_are_read_and_their_identities_counted(tmp_path):
    # S2 fix1 row F2c
    r = Repo(tmp_path)
    tip = r.base
    t = r.tree([(b"100644", b"README", r.readme)])
    for i in range(VC.SECTION_LINES + 1):
        tip = r.commit(t, [tip], b"c%d\n" % i)
    lines = section(r.preview(r.export(tip, [r.base])), "commits")
    assert lines[-3:] == ["(more commits): 1 more not listed, 0 flagged listed above",
                          "  author " + w("a <a@a>") + ": 1",
                          "  committer " + w("a <a@a>") + ": 1"]


@needs_git
def test_a_surface_added_then_deleted_is_listed_with_each_commit_that_touched_it(tmp_path):
    # S2 fix1 row F1 (complement MED 1): the net diff holds none of these
    r = Repo(tmp_path)
    k1 = r.commit(r.files({b".claude/settings.json": (b"100644", r.blob(b"{}\n")),
                           b"up": (b"120000", r.blob(b"/etc/passwd")),
                           b"vendor/b/HEAD": (b"100644", r.blob(b"ref: x\n")),
                           b"vendor/b/objects/x": (b"100644", r.blob(b"o\n")),
                           b"vendor/b/refs/y": (b"100644", r.blob(b"r\n"))}), [r.base], b"add\n")
    k2 = r.commit(r.files({}), [k1], b"del\n")
    text = r.preview(r.export(k2, [r.base]))
    cfg, links = section(text, "config surface"), section(text, "symlinks")
    assert f"commit {k1} A 100644 {w('.claude/settings.json')}  [.claude/]" in cfg
    assert f"commit {k2} D 100644 {w('.claude/settings.json')}  [.claude/]" in cfg
    assert f"commit {k1} A 040000 {w('vendor/b')}/  [embedded bare repository]" in cfg
    assert f"commit {k1} A {w('up')} -> {w('/etc/passwd')}  [points outside the repository]" in links
    assert text.count("safe.bareRepository") == 1
    assert section(text, "changes base..head", 2) == ["== changes base..head (0) ==", "none"]


@needs_git
def test_a_single_commit_delta_lists_each_surface_once(tmp_path):
    # the per-commit pass skips what a net list already holds (same status, path, mode, oid)
    r = Repo(tmp_path)
    text = r.preview(r.delta({b".claude/settings.json": (b"100644", r.blob(b"{}\n"))}))
    assert section(text, "config surface", 2) == ["== config surface (1) ==",
                                                  f"base..head A 100644 {w('.claude/settings.json')}  [.claude/]"]


@needs_git
def test_right_to_left_fields_and_look_alike_surfaces_are_flagged(tmp_path):
    # S2 fix1 row F3 (complement MED/LOW 3)
    heb, heb_t = "links/שלום".encode(), "../ירושלים".encode()
    cyr = ".clаude/settings.json".encode()
    r = Repo(tmp_path)
    text = r.preview(r.delta({heb: (b"120000", r.blob(heb_t)), cyr: (b"100644", r.blob(b"{}\n"))}))
    assert f"base..head A {w(heb.decode())} -> {w(heb_t.decode())}  [right-to-left text]" in \
        section(text, "symlinks", 3)
    assert f"base..head A 100644 {w(cyr.decode())}  [looks like .claude/]" in section(text, "config surface", 3)


@needs_git
def test_a_commit_past_commit_object_max_is_refused_by_name_not_shown_cut(tmp_path):
    # S2 fix1 row F4 (codex MED): the verifier's own name and detail for it (fsck_reader)
    r = Repo(tmp_path)
    name, detail = r.preview(r.delta({b"a.txt": (b"100644", r.blob(b"a\n"))}, msg=b"m" * 200 + b"\n"),
                             patch={"COMMIT_OBJECT_MAX": 100}, detail=True)
    assert name == "preview-cap:commits" and detail.startswith("object-size: commit ")


@needs_git
def test_the_git_budget_counts_every_byte_git_writes(tmp_path, monkeypatch):
    # S2 fix1 row F5 (codex MED): the budget's count == the bytes read off every git stdout inside write_preview
    # (the pumps' os.read, and cat-file's stdout through a counting proxy: both headers, payload, newline)
    from levain.slot import fsck_reader as FR
    got = {"read": 0, "catfile": 0, "on": False, "stats": None}
    real_read, real_init, real_wp = os.read, FR._Store.__init__, AP.write_preview

    def counting_read(fd, n):
        b = real_read(fd, n)
        if got["on"]:
            got["read"] += len(b)
        return b

    class Proxy:
        def __init__(self, f):
            self.f = f

        def readline(self, *a):
            b = self.f.readline(*a)
            got["catfile"] += len(b)
            return b

        def read(self, *a):
            b = self.f.read(*a)
            got["catfile"] += len(b)
            return b

        def close(self):
            got["catfile"] += len(self.f.read() or b"")      # anything left unread was still written by git
            self.f.close()

    def init(self, *a, **k):
        real_init(self, *a, **k)
        if got["on"]:
            self.p.stdout = Proxy(self.p.stdout)

    def wp(*a, **k):
        got["on"] = True
        try:
            got["stats"] = real_wp(*a, **k)
            return got["stats"]
        finally:
            got["on"] = False
    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(FR._Store, "__init__", init)
    monkeypatch.setattr(AP, "write_preview", wp)
    r = Repo(tmp_path)
    r.preview(r.delta({b"links/up": (b"120000", r.blob(b"../x")),
                       b"many/f%d" % 1: (b"100644", r.blob(b"e\n")), b".envrc": (b"100644", r.blob(b"x\n"))}))
    assert got["stats"].git_bytes == got["read"] + got["catfile"] and got["catfile"] > 0


@needs_git
def test_every_collision_pair_is_listed(tmp_path):
    # S2 fix1 row F6a (complement + codex LOW): three alike siblings are three pairs. U+017F LATIN SMALL LETTER LONG
    # S casefolds to "s" and has no canonical decomposition, so its pairs are case-fold, not NFC.
    long_s = "d/ſ"
    r = Repo(tmp_path)
    text = r.preview(r.delta({b"X": (b"100644", r.blob(b"1\n")), b"x": (b"100644", r.blob(b"2\n")),
                              b"d/S": (b"100644", r.blob(b"3\n")), b"d/s": (b"100644", r.blob(b"4\n")),
                              long_s.encode(): (b"100644", r.blob(b"5\n"))}))
    # ls-tree's order: X, d, d/S, d/s, d/<long s>, x, so X <-> x comes last
    assert section(text, "collisions", 5) == [
        "== collisions (4) ==", f"head case-fold {w('d/S')} <-> {w('d/s')}",
        f"head case-fold {w('d/S')} <-> {w(long_s)}", f"head case-fold {w('d/s')} <-> {w(long_s)}",
        f"head case-fold {w('X')} <-> {w('x')}"]


@needs_git
def test_collisions_skip_pairs_the_base_already_held_and_the_walk_has_its_own_cap(tmp_path):
    # S2 fix1 row F6d: the base holds A.txt + a.txt; the delta adds B.txt + b.txt
    r = Repo(tmp_path)
    r.readme = r.blob(b"base\n")
    tA, ta = r.blob(b"A\n"), r.blob(b"a\n")
    base_tree = r.tree([(b"100644", b"A.txt", tA), (b"100644", b"README", r.readme), (b"100644", b"a.txt", ta)])
    r.base = r.commit(base_tree, [])
    g("update-ref", "refs/heads/main", r.base, cwd=r.path)
    r.base_bundle.unlink()
    g("bundle", "create", "-q", str(r.base_bundle), "HEAD", "main", cwd=r.path)
    r.made.clear()
    tip = r.commit(r.files({b"A.txt": (b"100644", tA), b"a.txt": (b"100644", ta),
                            b"B.txt": (b"100644", r.blob(b"B\n")), b"b.txt": (b"100644", r.blob(b"b\n"))}), [r.base])
    bun = r.export(tip, [r.base])
    assert section(r.preview(bun), "collisions", 2) == ["== collisions (1) ==",
                                                       f"head case-fold {w('B.txt')} <-> {w('b.txt')}"]
    # 2 net + 2 per-commit change records; the tip walk (5 entries) no longer counts as listed paths
    assert r.preview(bun, patch={"LISTED_PATHS_CAP": 4}).startswith(AV.PREVIEW_HEADER)
    name, detail = r.preview(bun, patch={"TREE_WALK_ENTRIES_CAP": 3}, detail=True)
    assert name == "preview-cap:output" and "TREE_WALK_ENTRIES_CAP" in detail


@needs_git
def test_a_root_tree_holding_the_bare_pattern_is_flagged(tmp_path):
    # S2 fix1 row F6c (complement LOW 5)
    r = Repo(tmp_path)
    text = r.preview(r.delta({b"HEAD": (b"100644", r.blob(b"ref: x\n")), b"objects/x": (b"100644", r.blob(b"o\n")),
                              b"refs/y": (b"100644", r.blob(b"r\n"))}))
    assert "head root tree  [embedded bare repository]" in section(text, "config surface", 5)
    assert text.count("safe.bareRepository") == 1


@needs_git
def test_a_wip_off_head_gets_its_own_banner(tmp_path):
    # S2 fix1 row F6e (complement LOW 7)
    r = Repo(tmp_path)
    hc = r.commit(r.files({b"h": (b"100644", r.blob(b"h\n"))}), [r.base])
    wc = r.commit(r.files({b"w": (b"100644", r.blob(b"w\n"))}), [r.base])
    lines = r.preview(r.export_refs([(hc, "head"), (wc, "wip")], [r.base])).splitlines()
    assert f"!!! NOT ON HEAD: wip {wc} does not descend from head {hc}; every new commit is listed below !!!" in lines
    assert not [ln for ln in lines if ln.startswith("!!! NOT ON BASE")]


@needs_git
def test_config_surfaces_match_any_case_and_an_embedded_bare_repo_gets_one_hint(tmp_path):
    # RUN row E4
    r = Repo(tmp_path)
    text = r.preview(r.delta({
        b".gitmodules": (b"100644", r.blob(b'[submodule "x"]\n\tpath = x\n\turl = https://example.com/x\n')),
        b"vendor/inner/HEAD": (b"100644", r.blob(b"ref: x\n")),
        b"vendor/inner/config": (b"100644", r.blob(b"[core]\n\tbare = true\n")),
        b"vendor/inner/objects/info/packs": (b"100644", r.blob(b"\n")),
        b"vendor/inner/refs/heads/main": (b"100644", r.blob(b"0" * 40 + b"\n")),
        b".Claude/Settings.json": (b"100644", r.blob(b"{}\n")), b"sub/CLAUDE.MD": (b"100644", r.blob(b"c\n")),
        b".VSCode/Tasks.json": (b"100644", r.blob(b"{ }\n")), b"Makefile": (b"100644", r.blob(b"all:\n")),
        b"src/ok.py": (b"100644", r.blob(b"x = 1\n"))}))
    assert section(text, "config surface", 8) == [
        "== config surface (6) ==", f"base..head A 100644 {w('.Claude/Settings.json')}  [.claude/]",
        f"base..head A 100644 {w('.VSCode/Tasks.json')}  [.vscode/tasks.json]",
        f"base..head A 100644 {w('.gitmodules')}  [.git*]", f"base..head A 100644 {w('Makefile')}  [Makefile]",
        f"base..head A 100644 {w('sub/CLAUDE.MD')}  [CLAUDE*.md]",
        f"base..head A 040000 {w('vendor/inner')}/  [embedded bare repository]", AP.BARE_HINT]
    assert text.count("safe.bareRepository") == 1 and w("src/ok.py") + "  [" not in text


@needs_git
def test_a_clean_branch_says_no_config_surface_changed_and_has_no_banner(tmp_path):
    # RUN row E7
    r = Repo(tmp_path)
    lines = r.preview(r.delta({b"a.txt": (b"100644", r.blob(b"a\n"))})).splitlines()
    assert AP.NO_SURFACE in lines and not [ln for ln in lines if ln.startswith("!!!")]
    assert lines[lines.index("== symlinks (0) ==") + 1] == "none"


@needs_git
def test_collision_pairs_and_symlink_targets_are_listed(tmp_path):
    # RUN rows E5 and E6
    r = Repo(tmp_path)
    text = r.preview(r.delta({b"README.md": (b"100644", r.blob(b"1\n")), b"readme.md": (b"100644", r.blob(b"2\n")),
                              b"caf\xc3\xa9": (b"100644", r.blob(b"3\n")), b"cafe\xcc\x81": (b"100644", r.blob(b"4\n")),
                              b"links/up": (b"120000", r.blob(b"../../etc")),
                              b"links/in": (b"120000", r.blob(b"../README"))}))
    nfd, nfc = b"cafe\xcc\x81".decode(), b"caf\xc3\xa9".decode()
    assert section(text, "collisions", 3) == ["== collisions (2) ==", f"head NFC {w(nfd)} <-> {w(nfc)} (6 vs 5 bytes)",
                                              f"head case-fold {w('README.md')} <-> {w('readme.md')}"]
    assert section(text, "symlinks", 3) == [
        "== symlinks (2) ==", f"base..head A {w('links/in')} -> {w('../README')}",
        f"base..head A {w('links/up')} -> {w('../../etc')}  [points outside the repository]"]


@needs_git
@pytest.mark.parametrize("case", ["orphan", "reset"])
def test_a_head_off_base_gets_the_loud_banner(tmp_path, case):
    # RUN rows E8 and E9
    r = Repo(tmp_path, base_parents=1 if case == "reset" else 0)
    if case == "orphan":          # UNRELATED sends its whole closure: a tree of its own, no base blob
        t = r.tree([(b"100644", b"o.txt", r.blob(b"o\n"))])
        text, want = r.preview(r.export(r.commit(t, []), []), result="unrelated"), "!!! UNRELATED TO BASE: head "
    else:
        t = r.tree([(b"100644", b"README", r.blob(b"three\n"))])     # the RUN's E9 shape: no base blob named
        text, want = r.preview(r.export(r.commit(t, [r.first]), [r.first])), "!!! NOT ON BASE: head "
    assert text.splitlines()[4].startswith(want)


@needs_git
@pytest.mark.parametrize("cap,value", [("EXPANDED_OUTPUT_CAP", 2048), ("LISTED_PATHS_CAP", 200)])
def test_the_preview_caps_refuse_and_leave_no_file(tmp_path, monkeypatch, cap, value):
    # RUN rows E10 and E11, each cap read at its check
    r = Repo(tmp_path)
    bun = r.delta({b"many/f%03d" % i: (b"100644", r.blob(b"%d\n" % i)) for i in range(201)})
    assert r.preview(bun, patch={cap: value}, monkeypatch=monkeypatch) == "preview-cap:output"


@needs_git
def test_absent_symlink_targets_over_a_tiny_pack_refuse_by_name_not_by_the_share(tmp_path):
    # carried L3 r2 complement LOW 2 (RUN row C1; red on c46d8a09 as preview-cap:output, s2_c1_red.txt): an absent
    # special was written to required-unsorted twice, from its ``?<oid>`` line and again as a special
    r = Repo(tmp_path)
    absent = [hashlib.sha1(b"absent %d" % i).hexdigest() for i in range(3)]
    root = r.tree([(b"100644", b"README", r.readme)] + [(b"120000", b"l%d" % i, o) for i, o in enumerate(absent)])
    r.made = [o for o in r.made if o not in absent]
    with pytest.raises(Refused) as e:
        r.preview(r.export(r.commit(root, [r.base]), [r.base]))
    assert e.value.name == "bundle-special-unsent"


@needs_git
def test_the_child_writes_the_preview_under_the_real_entry(tmp_path):
    # RUN row E12: verify_run under the real export-verifier entry, through spawn_export's fd map
    from tests import test_slot_s5d1_verify as T
    r = Repo(tmp_path)
    bun = r.delta({b"a.txt": (b"100644", r.blob(b"a\n"))}, msg=HOSTILE)
    fx = T.Fx.__new__(T.Fx)
    fx.tmp, fx.base_bundle, fx.base = tmp_path / "child", r.base_bundle, r.base
    fx.tmp.mkdir()
    saved, T.S = T.S, S
    try:
        oc, outs, v = fx.run("hostile", bun)
    finally:
        T.S = saved
    assert oc.success and outs == ["preview.txt", "q.idx", "verified.json"]
    data = (v / "out" / "preview.txt").read_bytes()
    assert data.decode().splitlines()[0] == AV.PREVIEW_HEADER and HOSTILE_SHOWN.encode() in data
    assert b"\x1b" not in data


def test_the_escaper_shows_bad_utf8_and_hidden_characters_and_caps_before_escaping():
    assert AP.escape(b"a\xffb\\c\xe2\x80\xaed") == "a\\xffb\\\\c\\u202ed"
    assert AP.escape("x\U000e0001".encode()) == "x\\U000e0001"
    long = b"\x1b" * (VC.DISPLAY_FIELD_MAX + 1)
    assert AP.shown(long) == "\\u001b" * VC.DISPLAY_FIELD_MAX + \
        f" [{len(long)} bytes, sha256 {hashlib.sha256(long).hexdigest()[:16]}]"


@pytest.mark.parametrize("path,rule", [
    (b".CLAUDE/x", ".claude/"), (b"a/.Codex/y", ".codex/"), (b".agents", ".agents/"), (b".mcp.json", ".mcp.json"),
    (b"Claude-local.md", "CLAUDE*.md"), (b"docs/AGENTS.override.md", "AGENTS*.md"), (b".envrc", ".envrc"),
    (b".github/workflows/ci.yml", ".git*"), (b".GitAttributes", ".git*"), (b"GNUmakefile", "Makefile"),
    (b"rules.mk", "Makefile"), (b"web/package.json", "package.json (scripts)"), (b".husky/pre-push", ".husky/"),
    (b".vscode/tasks.json", ".vscode/tasks.json"), (b".pre-commit-config.yaml", ".pre-commit-config.yaml"),
    (b"tests/conftest.py", "conftest.py"), (b"Jenkinsfile", "CI"), (b"src/main.py", None),
    (b".vscode/settings.json", None)])
def test_every_config_surface_name_matches_on_a_path_component(path, rule):
    assert AP.surface_rule(path) == rule


def test_the_report_section_shows_named_entries_as_a_proposal():
    import base64
    named = [{"path_b64": base64.b64encode(b"fifo\x1b").decode(), "type": "fifo"}]
    out = AP.report_section({"named": named, "truncated": True})
    shown_path = w("fifo\\u001b")
    assert out == ["== the importer's report: named entries (a proposal) (1) ==", f"{w('fifo')} {shown_path}",
                   "the importer's report says its list was truncated"]


@pytest.mark.parametrize("path,rule", [
    (".clаude/settings.json", ".claude/"), ("．ｃｌａｕｄｅ/x", ".claude/"),
    ("Mаkefile", "Makefile"), ("café/x", None), (".claude/x", None), ("src/ok.py", None)])
def test_a_non_ascii_path_that_looks_like_a_surface_is_named(path, rule):
    # S2 fix1 row F3: the Cyrillic a, fullwidth letters (NFKC); an ASCII or a matching path is surface_rule's
    assert AP.surface_lookalike(path.encode()) == rule


def test_isolates_in_the_input_are_escaped_and_rtl_is_detected():
    assert AP.field(b"a\xe2\x81\xa8b\xe2\x81\xa9") == AP.FSI + "a\\u2068b\\u2069" + AP.PDI
    assert AP.rtl("xש".encode()) and AP.rtl("١".encode()) and not AP.rtl("café".encode())


def test_the_lookalike_table_is_derived_and_holds_only_single_ascii_prototypes():
    from levain.slot import confusables_ascii as C
    assert C.LOOKALIKE["а"] == "a" and C.SOURCE_VERSION
    assert all(len(k) == 1 and ord(k) >= 0x80 and len(v) == 1 and v.isascii() for k, v in C.LOOKALIKE.items())


def test_nul_in_commit_is_a_closed_fsck_id_and_rides_the_verifier_wire():
    from levain.slot import export_run as ER, fsck_ids
    assert "nulInCommit" in fsck_ids.closed_ids() and "nulInCommit" not in fsck_ids.reader_ids()
    assert "fsck:nulInCommit" in ER.VERIFIER_NAMES


@needs_git
def test_a_merge_in_the_new_range_previews_and_its_merged_side_is_listed(tmp_path):
    # S2 fix2 row G1 (complement MED, unverified): on git 2.50.1 a merge's --stdin -m -z header is its bare oid
    r = Repo(tmp_path)
    cl = r.blob(b"{}\n")
    ks = r.commit(r.files({b".claude/settings.json": (b"100644", cl)}), [r.base], b"side\n")
    km = r.commit(r.files({b"m.txt": (b"100644", r.blob(b"m\n"))}), [r.base], b"main\n")
    kM = r.commit(r.files({b".claude/settings.json": (b"100644", cl), b"m.txt": (b"100644", r.blob(b"m\n"))}),
                  [km, ks], b"merge\n")
    kd = r.commit(r.files({b"m.txt": (b"100644", r.blob(b"m\n"))}), [kM], b"del\n")
    cfg = section(r.preview(r.export(kd, [r.base])), "config surface")
    for k, st in ((ks, "A"), (kM, "A"), (kd, "D")):
        assert f"commit {k} {st} 100644 {w('.claude/settings.json')}  [.claude/]" in cfg


@needs_git
def test_an_intermediate_commits_root_tree_holding_the_bare_pattern_is_listed(tmp_path):
    # S2 fix2 row G2 (codex 1)
    r = Repo(tmp_path)
    k1 = r.commit(r.files({b"HEAD": (b"100644", r.blob(b"ref: x\n")), b"objects/x": (b"100644", r.blob(b"o\n")),
                           b"refs/y": (b"100644", r.blob(b"r\n"))}), [r.base], b"bare\n")
    k2 = r.commit(r.files({}), [k1], b"unbare\n")
    text = r.preview(r.export(k2, [r.base]))
    assert f"commit {k1} root tree  [embedded bare repository]" in section(text, "config surface")
    assert text.count("safe.bareRepository") == 1


@needs_git
def test_commits_past_the_section_are_read_for_flags_and_flagged_identities_never_fold(tmp_path):
    # S2 fix2 row G3 (codex 2), SECTION_LINES patched to 2; rev-list order (newest first) is L0 .. L7
    r = Repo(tmp_path)
    plan = [(b"b1 <b@b> 0 +0000", "שלום עולם\n".encode()), ("שלום <h@h> 0 +0000".encode(), b"m\n"),
            ("аlice <a@a> 0 +0000".encode(), b"m\n"), (b"b3 <b@b> 0 +0000", b"m\n"), (b"b2 <b@b> 0 +0000", b"m\n"),
            (b"b1 <b@b> 0 +0000", b"m\n"), (b"a <a@a> 0 +0000", b"m\n"), (b"a <a@a> 0 +0000", b"m\n")]
    tip, made = r.base, []
    for author, msg in plan:
        tip = r.commit(r.base_tree, [tip], msg, author=author)
        made.append(tip)
    L = made[::-1]
    cm = section(r.preview(r.export(tip, [r.base]), patch={"SECTION_LINES": 2}), "commits")
    assert cm[0] == "== commits (8) =="
    assert [k for k in L if f"commit {k}" in cm] == [L[0], L[1], L[5], L[6], L[7]]
    assert "  author " + w("аlice <a@a> 0 +0000") + AP.LOOKS_ASCII in cm
    assert "  author " + w("שלום <h@h> 0 +0000") + AP.RTL in cm
    assert "    " + w("שלום עולם") + AP.RTL in cm
    assert cm[-7:] == ["(more commits): 3 more not listed, 3 flagged listed above",
                       "  author " + w("b1 <b@b>") + ": 2", "  author " + w("b2 <b@b>") + ": 1",
                       "  author " + w("аlice <a@a>") + ": 1" + AP.LOOKS_ASCII,
                       "  author " + w("שלום <h@h>") + ": 1" + AP.RTL,
                       "  (other identities): 1 more not listed, 2 flagged listed above",
                       "  committer " + w("a <a@a>") + ": 6"]


@needs_git
def test_a_collision_the_base_held_is_not_new_when_content_beneath_it_changes(tmp_path):
    # S2 fix2 row G5 (codex 6): the base holds Dir/ + dir/ and A.txt + a.txt; the delta changes content under both
    r = Repo(tmp_path)
    x, y, tA, ta = r.blob(b"x\n"), r.blob(b"y\n"), r.blob(b"A\n"), r.blob(b"a\n")
    held = {b"Dir/x": (b"100644", x), b"dir/y": (b"100644", y), b"A.txt": (b"100644", tA), b"a.txt": (b"100644", ta)}
    r.base_tree = r.files(held)
    r.base = r.commit(r.base_tree, [])
    g("update-ref", "refs/heads/main", r.base, cwd=r.path)
    r.base_bundle.unlink()
    g("bundle", "create", "-q", str(r.base_bundle), "HEAD", "main", cwd=r.path)
    keep = set(r.made)
    r.made.clear()
    tip = r.commit(r.files({**held, b"Dir/x": (b"100644", r.blob(b"x2\n")), b"A.txt": (b"100644", r.blob(b"A2\n")),
                            b"DIR/z": (b"100644", r.blob(b"z\n"))}), [r.base])
    r.made[:] = [o for o in r.made if o not in keep]
    assert section(r.preview(r.export(tip, [r.base])), "collisions") == [
        "== collisions (2) ==", f"head case-fold {w('DIR')} <-> {w('Dir')}",
        f"head case-fold {w('DIR')} <-> {w('dir')}"]


def test_the_unicode_license_is_in_the_repo_and_the_table_points_at_it():
    # S2 fix2 row G6 (head Q4)
    from levain.slot import confusables_ascii as C
    root = Path(__file__).resolve().parent.parent
    assert (root / "LICENSES" / "Unicode-3.0.txt").read_text(encoding="utf-8").startswith("UNICODE LICENSE V3\n")
    assert "LICENSES/Unicode-3.0.txt" in (C.__doc__ or "")


def test_an_identity_that_reads_as_ascii_is_flagged_and_another_script_is_not():
    assert AP.ascii_lookalike("аlice".encode()) and not AP.ascii_lookalike(b"alice")
    assert not AP.ascii_lookalike("Иван".encode()) and not AP.ascii_lookalike("José".encode())
