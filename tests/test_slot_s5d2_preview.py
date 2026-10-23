"""S1 session 5 piece 5d-ii session 2: §6.2 content, ``out/preview.txt`` (:mod:`levain.slot.adopt_preview`).

The rows come from the session's RUN (lane adopt5dii-s2, ``1022-21/adopt5dii/s2_run.txt``, the expected text in that
dir's ``s2_expected.md``, written before it ran). Objects are written loose by the rig (any byte can sit in a commit
or a name), the export bundle is ``bundlefmt.write_header`` + ``pack-objects``, and every row runs on the host git.
"""
import hashlib
import shutil
import subprocess
import unicodedata
import zlib
from pathlib import Path

import pytest

from levain.slot import Refused, importer
from levain.slot import adopt_preview as AP, adopt_verify as AV, bundlefmt as BF, verify_caps as VC

GIT = shutil.which("git")
S = "s-" + "d3" * 8
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C"}
needs_git = pytest.mark.skipif(GIT is None, reason="needs a git")
HOSTILE = b"subj \x1b[31mred\x1b[0m \xe2\x80\xae evil \xc2\x9b c1 \x9b raw \x01\x7f\x1b[2J nul-adjacent \\ back\n"
HOSTILE_SHOWN = ("    subj \\u001b[31mred\\u001b[0m \\u202e evil \\u009b c1 \\x9b raw \\u0001\\u007f\\u001b[2J "
                 "nul-adjacent \\\\ back")


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
        self.base = self.commit(self.tree([(b"100644", b"README", self.readme)]), parents)
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
        oids = [o for o in dict.fromkeys(self.made) if o not in (self.readme, self.base, self.first)]
        pk = g("pack-objects", "-q", "--stdout", cwd=self.path, stdin=b"".join(o.encode() + b"\n" for o in oids))
        p = self.tmp / "export.bundle"
        p.write_bytes(BF.write_header("sha1", [(tip, f"refs/levain/{S}/head".encode())], list(prereqs)) + pk)
        return p

    def delta(self, files, **kw) -> Path:
        return self.export(self.commit(self.files(files), [self.base], **kw), [self.base])

    def preview(self, bundle: Path, result: str = "delta", patch=None, monkeypatch=None) -> str:
        """quarantine_verify, then write_preview over its P; the text, or the refusal's name."""
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        out = self.tmp / "preview.txt"
        v = AV.quarantine_verify(bundle, self.base_bundle, session=S, base_oid=self.base, object_format="sha1",
                                 result=result, scratch=scratch, git=GIT, max_input_size=1 << 30)
        for k, val in (patch or {}).items():
            monkeypatch.setattr(VC, k, val)
        try:
            AP.write_preview(out, importer.GitRunner(GIT, scratch / "p", None), v, base_oid=self.base, hexlen=40,
                             path_max=1024)
        except Refused as r:
            assert not out.exists()
            return r.name
        return out.read_text(encoding="utf-8")


def section(text: str, title: str, n: int) -> list:
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"== {title} ("))
    return lines[i:i + n]


@needs_git
def test_a_hostile_commit_is_escaped_and_no_hidden_character_reaches_the_file(tmp_path):
    # RUN row E1: terminal escapes, U+202E, C1 as UTF-8 and as a raw byte, the bytes beside a NUL, a backslash
    r = Repo(tmp_path)
    text = r.preview(r.delta({b"a.txt": (b"100644", r.blob(b"a\n"))}, msg=HOSTILE,
                             author=b"ev\x1b]0;t\x07il <e@x> 0 +0000"))
    lines = text.splitlines()
    assert lines[0] == AV.PREVIEW_HEADER
    assert "  author ev\\u001b]0;t\\u0007il <e@x> 0 +0000" in lines and HOSTILE_SHOWN in lines
    assert not [c for c in text if (ord(c) < 0x20 and c != "\n") or 0x7f <= ord(c) <= 0x9f
                or unicodedata.category(c) == "Cf"]


@needs_git
def test_a_nul_in_a_commit_message_is_refused_before_the_preview(tmp_path):
    # RUN row E1n (written after run 1): index-pack --fsck-objects says nulInCommit, which is not in the closed list
    r = Repo(tmp_path)
    with pytest.raises(Refused) as e:
        r.preview(r.delta({}, msg=HOSTILE.replace(b"\x01\x7f", b"\x00")))
    assert e.value.name == "fsck:other"


@needs_git
def test_a_long_path_is_capped_before_escaping_and_warned_past_path_max(tmp_path):
    # RUN row E2
    long = b"/".join([b"p" * 1023] * 5 + [b"ffff"])
    r = Repo(tmp_path)
    lines = r.preview(r.delta({long: (b"100644", r.blob(b"x\n"))})).splitlines()
    assert f"A 100644 {long[:4096].decode()} [{len(long)} bytes, sha256 {hashlib.sha256(long).hexdigest()[:16]}]" \
        in lines
    assert f"  warning: {len(long)} bytes is longer than this OS's PATH_MAX (1024): a checkout may fail" in lines


@needs_git
def test_a_section_keeps_its_first_entries_then_groups_the_rest(tmp_path):
    # RUN row E3
    r = Repo(tmp_path)
    n = VC.SECTION_LINES + 1
    text = r.preview(r.delta({b"many/f%03d" % i: (b"100644", r.blob(b"%d\n" % i)) for i in range(n)}))
    assert section(text, "changes base..head", n + 1) == [f"== changes base..head ({n}) =="] + \
        [f"A 100644 many/f{k:03d}" for k in range(n - 1)] + ["many/: 1 more"]


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
        "== config surface (6) ==", "base..head A 100644 .Claude/Settings.json  [.claude/]",
        "base..head A 100644 .VSCode/Tasks.json  [.vscode/tasks.json]", "base..head A 100644 .gitmodules  [.git*]",
        "base..head A 100644 Makefile  [Makefile]", "base..head A 100644 sub/CLAUDE.MD  [CLAUDE*.md]",
        "base..head A 040000 vendor/inner/  [embedded bare repository]", AP.BARE_HINT]
    assert text.count("safe.bareRepository") == 1 and "src/ok.py  [" not in text


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
    assert section(text, "collisions", 3) == ["== collisions (2) ==", "head NFC café <-> café (6 vs 5 bytes)",
                                              "head case-fold README.md <-> readme.md"]
    assert section(text, "symlinks", 3) == ["== symlinks (2) ==", "base..head A links/in -> ../README",
                                            "base..head A links/up -> ../../etc  [points outside the repository]"]


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
        text, want = r.preview(r.export(r.commit(t, [r.first]), [r.first])), \
            "!!! NOT ON BASE (rewound to an ancestor): head "
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
    assert out == ["== the importer's report: named entries (a proposal) (1) ==", "fifo fifo\\u001b",
                   "the importer's report says its list was truncated"]
