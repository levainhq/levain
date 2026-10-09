"""Keeping process views out of the entity's reach (lane P2's research note, item 2c).

The file editor runs inside levain's own process, so ``/proc/self/mem`` and ``/proc/<pid>/environ``
reached levain's memory and other processes' environments; on Linux bash saw every process of the
user through the sandbox's procfs. These tests read the predicate, the rendered bwrap argv and the
mount table as data. The live check (``test_linux_live_bash_sees_no_host_process``) runs only where
bwrap can establish a namespace.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import BwrapProvider, build_policy, crown_jewel_reason


@pytest.fixture
def policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    return build_policy(ent)


@pytest.mark.parametrize("path", [
    "/proc/self/environ", "/proc/self/mem", "/proc/1/cmdline", "/proc/self/fd/3", "/proc",
    "/dev/fd/3", "/dev/fd", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/PROC/self/environ",
    "/proc/../proc/self/environ",
])
def test_the_file_editor_refuses_process_views(policy, path: str) -> None:
    assert crown_jewel_reason(policy, path) is not None, path


@pytest.mark.parametrize("path", ["/procfoo/x", "/dev/null", "/dev/fdx", "/tmp/proc/self/environ"])
def test_the_rule_is_anchored(policy, path: str) -> None:
    assert crown_jewel_reason(policy, path) is None, path


def test_a_link_to_a_process_view_is_refused_at_its_target(policy, tmp_path: Path) -> None:
    link = tmp_path / "work" / "env"
    link.parent.mkdir()
    link.symlink_to("/proc/self/environ")
    assert crown_jewel_reason(policy, link) is not None


def test_bwrap_runs_bash_in_its_own_pid_namespace(policy) -> None:
    argv = C._bwrap_argv(policy)
    assert "--unshare-pid" in argv
    assert "--unshare-pid" in BwrapProvider().render_profile(policy)


_MOUNTINFO = """\
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
23 22 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
24 23 0:22 / /proc/sys/fs/binfmt_misc rw,relatime shared:13 - autofs systemd-1 rw
25 22 0:21 / /srv/chroot/proc rw,relatime - proc proc rw
26 22 0:21 / /var/lib/my\\040box/proc rw,relatime - proc proc rw
27 23 0:21 / /proc/x rw,relatime - proc proc rw
28 22 0:30 / /run rw,nosuid shared:5 - tmpfs tmpfs rw
"""


def test_the_mount_table_scan_finds_procfs_outside_proc(tmp_path: Path) -> None:
    mi = tmp_path / "mountinfo"
    mi.write_text(_MOUNTINFO)
    assert C._extra_procfs_mounts(str(mi)) == [Path("/srv/chroot/proc"), Path("/var/lib/my box/proc")]


def test_no_mount_table_means_none_and_a_garbled_one_refuses(tmp_path: Path) -> None:
    assert C._extra_procfs_mounts(str(tmp_path / "absent")) == []
    bad = tmp_path / "mountinfo"
    bad.write_text("22 1 8:1 / / rw\n")
    with pytest.raises(OSError):
        C._extra_procfs_mounts(str(bad))


def test_the_plan_hides_every_other_procfs_read_only(policy, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "chroot" / "proc"
    other.mkdir(parents=True)
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [other])
    argv = C._bwrap_argv(policy)
    i = argv.index(str(other))
    assert argv[i - 1] == "--tmpfs"
    assert str(other) in [argv[k + 1] for k, a in enumerate(argv) if a == "--remount-ro"]


def test_a_garbled_mount_table_refuses_bash(policy, monkeypatch) -> None:
    def boom():
        raise OSError("unreadable mount table line")
    monkeypatch.setattr(C, "_extra_procfs_mounts", boom)
    with pytest.raises(C.ConfinementError, match="mount table"):
        C._bwrap_argv(policy)


def test_the_availability_probes_ask_for_a_pid_namespace(monkeypatch) -> None:
    seen: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(C, "BWRAP", sys.executable)   # any executable file: the probe only runs it
    monkeypatch.setattr(C.subprocess, "run", lambda argv, **kw: (seen.append(argv), _Done())[1])
    assert C.bwrap_available() and C.bwrap_netns_available()
    assert all("--unshare-pid" in argv for argv in seen) and len(seen) == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl(PR_SET_DUMPABLE) is Linux")
def test_levain_marks_itself_not_dumpable_on_linux() -> None:
    code = ("import ctypes; from levain.launch import set_not_dumpable; ok = set_not_dumpable(); "
            "print(ok, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))")   # 3 = PR_GET_DUMPABLE
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.getcwd()})
    assert out.stdout.split() == ["True", "0"], out.stderr


def test_not_dumpable_is_a_no_op_off_linux(monkeypatch) -> None:
    from levain import launch

    monkeypatch.setattr(launch.sys, "platform", "darwin")
    assert launch.set_not_dumpable() is False


_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and C.bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


@_live
def test_linux_live_bash_sees_no_host_process(policy) -> None:
    shell = BwrapProvider().spawn_shell(policy)
    try:
        r = shell.run(f"test -e /proc/{os.getpid()}/environ && echo VISIBLE || echo HIDDEN; "
                      "ls /proc | grep -c '^[0-9]'")
    finally:
        shell.close()
    lines = r.output.split()
    assert lines[0] == "HIDDEN"
    assert int(lines[1]) < 10, "only the sandbox's own few processes are listed"


def test_the_spelling_is_checked_as_given_not_only_resolved() -> None:
    # On Linux /proc/self/fd/N resolves to the file fd N has open, which may be an ordinary path.
    assert C._process_view_reason("/proc/self/fd/7", Path("/home/u/work/notes.txt")) is not None
    assert C._process_view_reason("/home/u/work/notes.txt", Path("/home/u/work/notes.txt")) is None


# --- the file editor judges the object it opened, not only the path it was given -----------------------


def test_a_link_flipped_after_the_path_check_is_refused_at_the_open(tmp_path: Path, monkeypatch) -> None:
    """The race, made deterministic: the path check sees a benign file, and by the time the editor
    opens the path it leads to a crown jewel."""
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel_dir = tmp_path / ".anneal-memory"
    jewel_dir.mkdir()
    (jewel_dir / "secret.txt").write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "notes.txt"
    link.write_text("benign\n")
    ex = T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    real_check = T.crown_jewel_reason

    def check_then_flip(policy, path):
        out = real_check(policy, path)
        if Path(path) == link:          # the shell swaps the file for a link right after the check
            link.unlink()
            link.symlink_to(jewel_dir / "secret.txt")
        return out
    monkeypatch.setattr(T, "crown_jewel_reason", check_then_flip)
    obs = ex(FileEditorAction(command="view", path=str(link)))
    text = "".join(getattr(c, "text", "") for c in obs.to_llm_content)
    assert "JEWEL-CONTENT" not in text
    # The editor's own existence check now answers through the floor, so the flipped path reads as
    # missing, the same answer a missing path gets (head ruling 2026-10-07); the open refuses either way.
    assert obs.is_error and ("crown" in text.lower() or "does not exist" in text)


def test_the_open_check_is_inert_outside_a_floored_call(tmp_path: Path) -> None:
    from levain.firing.openhands import tools as T

    f = tmp_path / "x"
    f.write_text("ok")
    with T._floored_open(f) as fh:
        assert fh.read() == "ok"


def test_opened_file_reason_names_a_jewel_by_the_opened_object(policy, tmp_path: Path) -> None:
    jewel = tmp_path / ".anneal-memory" / "m.db"
    jewel.parent.mkdir(exist_ok=True)
    jewel.write_text("x")
    other = tmp_path / "plain"
    other.write_text("y")
    pol = build_policy(tmp_path / "ent")
    with open(jewel) as a, open(other) as b:
        assert C.opened_file_reason(pol, a.fileno()) is not None
        assert C.opened_file_reason(pol, b.fileno()) is None
    r, w = os.pipe()
    try:
        assert "could not be identified" in (C.opened_file_reason(pol, r) or "")
    finally:
        os.close(r)
        os.close(w)


def test_a_write_through_a_flipped_link_neither_truncates_nor_writes_the_jewel(tmp_path: Path,
                                                                                monkeypatch) -> None:
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel = tmp_path / ".anneal-memory" / "secret.txt"
    jewel.parent.mkdir()
    jewel.write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "new.txt"
    ex = T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    real_check = T.crown_jewel_reason

    def check_then_flip(policy, path):
        out = real_check(policy, path)
        if Path(path) == target and not target.is_symlink():
            target.symlink_to(jewel)
        return out
    monkeypatch.setattr(T, "crown_jewel_reason", check_then_flip)
    obs = ex(FileEditorAction(command="create", path=str(target), file_text="PLANTED"))
    assert obs.is_error
    assert jewel.read_text() == "JEWEL-CONTENT\n"


def test_a_refused_write_open_does_not_truncate_first(tmp_path: Path, monkeypatch) -> None:
    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel = tmp_path / ".anneal-memory" / "secret.txt"
    jewel.parent.mkdir()
    jewel.write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(jewel)
    token = T._EDITOR_FLOOR.set(build_policy(ent))
    try:
        with pytest.raises(T._FloorRefusedOpen):
            T._floored_open(link, "w")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert jewel.read_text() == "JEWEL-CONTENT\n"


def test_on_linux_any_other_procfs_counts_as_a_process_view(monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [Path("/srv/chroot/proc")])
    assert C._process_view_reason("/srv/chroot/proc/1/environ", Path("/srv/chroot/proc/1/environ"))
    assert C._process_view_reason("/srv/chroot/etc/hosts", Path("/srv/chroot/etc/hosts")) is None


def test_a_host_that_cannot_give_bash_a_pid_namespace_is_named_and_refused(monkeypatch) -> None:
    monkeypatch.setattr(C.os.path, "isfile", lambda p: True if p == C.BWRAP else os.path.lexists(p))
    monkeypatch.setattr(C.os, "access", lambda p, m: True if p == C.BWRAP else os.access(p, m))
    monkeypatch.setattr(C, "bwrap_available", lambda: False)   # what the diagnosis asks, once
    monkeypatch.setattr(C, "_bwrap_runs_without_a_pid_namespace", lambda: True)
    monkeypatch.setattr(C, "_cgroup_problem", lambda: None)
    d = C.diagnose_confinement("Linux")
    assert not d.supported and "PID namespace" in d.reason and "systempaths=unconfined" in d.remedy


def test_a_file_shaped_procfs_mount_is_masked_not_tmpfsd(policy, tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "bound-environ"
    f.write_text("")
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [f])
    argv = C._bwrap_argv(policy)
    triples = [argv[k:k + 3] for k in range(len(argv))]
    assert ["--ro-bind", "/dev/null", str(f)] in triples
    assert ["--tmpfs", str(f)] not in [argv[k:k + 2] for k in range(len(argv))]


# --- L3 r1 -------------------------------------------------------------------------------------------


def _under_floor(tmp_path: Path, monkeypatch):
    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True, exist_ok=True)
    return T, T._EDITOR_FLOOR.set(build_policy(ent))


def test_x_mode_still_refuses_an_existing_file(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    f = tmp_path / "ws.txt"
    f.write_text("KEEP")
    try:
        with pytest.raises(FileExistsError):
            T._floored_open(f, "x")
        with T._floored_open(tmp_path / "new.txt", "x") as fh:
            fh.write("ok")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert f.read_text() == "KEEP" and (tmp_path / "new.txt").read_text() == "ok"


def test_the_floored_stat_of_an_unreadable_file_answers_instead_of_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """A file the editor's process may not open (mode 000, or under ruling A a hands file the
    operator cannot read) is stat'ed and judged by identity; that path once named a function this
    module never imported, so it raised NameError instead of answering."""
    if os.geteuid() == 0:
        pytest.skip("root opens a mode-000 file")
    T, token = _under_floor(tmp_path, monkeypatch)
    f = tmp_path / "locked.txt"
    f.write_text("x")
    f.chmod(0)
    try:
        st = T._floored_stat(f)
    finally:
        T._EDITOR_FLOOR.reset(token)
        f.chmod(0o600)
    assert st is not None and st.st_size == 1


def test_a_planted_fifo_is_refused_without_blocking(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(T._FloorRefusedOpen, match="not a regular file"):
            T._floored_open(fifo)
    finally:
        T._EDITOR_FLOOR.reset(token)


def test_a_dangling_link_is_refused_not_followed_into_a_create(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "elsewhere" / "target")
    (tmp_path / "elsewhere").mkdir()
    try:
        with pytest.raises(T._FloorRefusedOpen, match="dangling"):
            T._floored_open(link, "w")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert not (tmp_path / "elsewhere" / "target").exists()


def test_the_patched_opens_are_the_editors_only_way_to_read_a_file() -> None:
    """The open-then-check rides the module-global `open` of two editor modules. A later SDK that
    reads or writes another way would bypass it silently, so this fails if one appears."""
    import inspect

    from openhands.tools.file_editor import editor
    from openhands.tools.file_editor.utils import encoding

    for mod in (editor, encoding):
        src = inspect.getsource(mod)
        for primitive in ("read_text(", "read_bytes(", "write_text(", "write_bytes(", "io.open(",
                          "os.open(", "codecs.open(", "mmap"):
            assert primitive not in src, f"{mod.__name__} uses {primitive}"


def test_a_file_another_process_creates_between_the_two_opens_is_opened(tmp_path: Path, monkeypatch) -> None:
    """L3 r2 (gemini + complement): a file created between the probe and the exclusive create was
    refused as a dangling symlink. It is opened again instead."""
    T, token = _under_floor(tmp_path, monkeypatch)
    f = tmp_path / "raced.txt"
    real_open = os.open
    raced = []

    def racing_open(path, flags, mode=0o777, **kw):
        if str(path) == str(f) and not raced and not flags & os.O_CREAT:
            raced.append(1)
            fd = real_open(path, os.O_CREAT | os.O_WRONLY, 0o644)   # the other process wins
            os.close(fd)
            raise FileNotFoundError(path)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(T.os, "open", racing_open)
    try:
        with T._floored_open(f, "w") as fh:
            fh.write("ok")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert raced and f.read_text() == "ok"


def test_a_jewel_opened_through_a_name_swapped_away_before_the_check_is_refused(
    policy, tmp_path: Path, monkeypatch
) -> None:
    """codex (L3 r2 frozen tip): the editor opened a hardlink to a jewel, the shell then replaced that
    name with a benign file, and the check re-stat'ed the NAME, so the opened jewel passed. The opened
    object's own identity decides now."""
    jewel = tmp_path / ".anneal-memory" / "m.db"
    jewel.parent.mkdir(exist_ok=True)
    jewel.write_text("SECRET")
    pol = build_policy(tmp_path / "ent")
    work = tmp_path / "work"
    work.mkdir()
    x = work / "x"
    os.link(jewel, x)
    with open(x) as fh:
        x.unlink()
        x.write_text("benign")                 # the name now leads to another file
        # How Linux names it (/proc/self/fd/N); macOS's F_GETPATH may name the jewel's other path.
        monkeypatch.setattr(C, "opened_file_path", lambda fd: f"{x} (deleted)")
        assert C.opened_file_reason(pol, fh.fileno()) is not None


# --- the editor's non-open primitives walk by directory fd (codex #2, #6; head ruling 2026-10-07) ----


def _jewel_dir_and_link(tmp_path: Path) -> tuple[Path, Path, Path]:
    jewel_dir = tmp_path / ".anneal-memory"
    jewel_dir.mkdir(exist_ok=True)
    (jewel_dir / "SECRET-NAME.md").write_text("JEWEL\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "link"
    link.symlink_to(jewel_dir)
    return jewel_dir, ws, link


def test_insert_moving_onto_a_path_through_a_symlinked_parent_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The move step of `insert` (shutil.move, not open): a parent link flipped to a jewel dir after
    the executor's check would have the temp file renamed onto the jewel."""
    from openhands.tools.file_editor import editor as E

    T, token = _under_floor(tmp_path, monkeypatch)
    jewel_dir, ws, link = _jewel_dir_and_link(tmp_path)
    tmp = tmp_path / "tmpfile"
    tmp.write_text("PLANTED\n")
    try:
        with pytest.raises(T._FloorRefusedWalk):
            E.shutil.move(str(tmp), link / "SECRET-NAME.md")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert (jewel_dir / "SECRET-NAME.md").read_text() == "JEWEL\n"


def test_view_of_a_symlinked_dir_into_a_denied_subtree_is_refused(tmp_path: Path, monkeypatch) -> None:
    from openhands.tools.file_editor.editor import FileEditor

    T, token = _under_floor(tmp_path, monkeypatch)
    _, ws, link = _jewel_dir_and_link(tmp_path)
    try:
        with pytest.raises(T._FloorRefusedWalk):
            FileEditor().view(link)
        out = str(FileEditor().view(ws))
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert "SECRET-NAME" not in out, "a link inside the listed dir is shown, never entered"
    assert "link" in out


def test_insert_and_view_still_work_on_ordinary_paths(tmp_path: Path, monkeypatch) -> None:
    from openhands.tools.file_editor.editor import FileEditor

    T, token = _under_floor(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "deep.txt").write_text("")
    f = ws / "a.txt"
    f.write_text("one\ntwo\n")
    try:
        FileEditor().insert(f, 1, "inserted")
        out = str(FileEditor().view(ws))
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert f.read_text() == "one\ninserted\ntwo\n"
    assert "a.txt" in out and "sub/" in out and "deep.txt" in out


def test_a_cross_filesystem_insert_replaces_the_name_and_a_refused_one_leaves_no_temp(
    tmp_path: Path, monkeypatch
) -> None:
    """L1 r3: the EXDEV copy opened the target in place (a FIFO hung it, a hardlink was written
    through), and a refused move left the edit behind in the temp file."""
    from openhands.tools.file_editor import editor as E

    T, token = _under_floor(tmp_path, monkeypatch)
    ws = tmp_path / "w"
    ws.mkdir()
    other = tmp_path / "other"
    other.write_text("UNTOUCHED\n")
    target = ws / "t.txt"
    os.link(other, target)                    # a hardlink at the target name
    src = tmp_path / "src"
    src.write_text("NEW\n")
    real_rename = os.rename

    def exdev_once(a, b, *args, **kw):
        if str(a) == str(src):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(a, b, *args, **kw)

    monkeypatch.setattr(T.os, "rename", exdev_once)
    try:
        E.shutil.move(str(src), target)
        _, _, link = _jewel_dir_and_link(tmp_path)
        refused_src = tmp_path / "src2"
        refused_src.write_text("EDIT\n")
        with pytest.raises(T._FloorRefusedWalk):
            E.shutil.move(str(refused_src), link / "x")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert target.read_text() == "NEW\n" and other.read_text() == "UNTOUCHED\n"
    assert not src.exists() and not refused_src.exists()


# --- the walk's trusted prefix and the floored stats (head rulings 2026-10-07) -----------------------


def _executor_through_a_linked_prefix(tmp_path: Path, monkeypatch):
    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "real"
    (real / "ws").mkdir(parents=True)
    lnk = tmp_path / "lnk"                     # what macOS /tmp is to /private/tmp
    lnk.symlink_to(real)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = lnk / "ws"
    return T, T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws)), ws


def _text(obs) -> str:
    return "".join(getattr(c, "text", "") for c in obs.to_llm_content)


def test_insert_and_view_under_a_workspace_reached_through_a_link_work(tmp_path: Path, monkeypatch) -> None:
    from openhands.tools.file_editor.definition import FileEditorAction

    T, ex, ws = _executor_through_a_linked_prefix(tmp_path, monkeypatch)
    f = ws / "a.txt"
    f.write_text("one\ntwo\n")
    (ws / "sub").mkdir()
    obs = ex(FileEditorAction(command="insert", path=str(f), insert_line=1, new_str="mid"))
    assert not obs.is_error, _text(obs)
    assert f.read_text() == "one\nmid\ntwo\n"
    obs = ex(FileEditorAction(command="view", path=str(ws)))
    assert not obs.is_error and "a.txt" in _text(obs) and "sub/" in _text(obs)


def test_a_link_planted_below_the_workspace_is_still_not_followed(tmp_path: Path, monkeypatch) -> None:
    from openhands.tools.file_editor.definition import FileEditorAction

    T, ex, ws = _executor_through_a_linked_prefix(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "NAME-BEYOND.txt").write_text("x")
    (ws / "planted").symlink_to(elsewhere)
    obs = ex(FileEditorAction(command="view", path=str(ws / "planted")))
    assert obs.is_error and "NAME-BEYOND" not in _text(obs)
    obs = ex(FileEditorAction(command="insert", path=str(ws / "planted" / "NAME-BEYOND.txt"),
                              insert_line=0, new_str="PLANT"))
    assert obs.is_error and (elsewhere / "NAME-BEYOND.txt").read_text() == "x"


def test_a_denied_jewel_answers_exactly_as_a_missing_path(tmp_path: Path, monkeypatch) -> None:
    """The editor's name-based checks ran in levain's own process, so a link flipped after the path
    check told the entity whether a jewel existed. Now an existing jewel and a missing file read the
    same."""
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel_dir = tmp_path / ".anneal-memory"
    jewel_dir.mkdir()
    (jewel_dir / "present.txt").write_text("J")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    ex = T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    real_check = T.crown_jewel_reason
    answers = []
    for target in ("present.txt", "absent.txt"):
        link = ws / "probe"
        link.write_text("benign")

        def check_then_flip(policy, path, _t=target):
            out = real_check(policy, path)
            if Path(path) == link and not link.is_symlink():
                link.unlink()
                link.symlink_to(jewel_dir / _t)
            return out

        monkeypatch.setattr(T, "crown_jewel_reason", check_then_flip)
        answers.append(_text(ex(FileEditorAction(command="view", path=str(link)))))
        monkeypatch.setattr(T, "crown_jewel_reason", real_check)
        link.unlink()
    assert answers[0] == answers[1], answers


def test_a_cross_filesystem_move_never_follows_a_swapped_temp_file(tmp_path: Path, monkeypatch) -> None:
    """codex closing pass: the EXDEV copy reopened the editor's temp file by NAME, in a directory the
    sandbox shares, so a temp file swapped for a link to a jewel was copied into the workspace."""
    from openhands.tools.file_editor import editor as E

    T, token = _under_floor(tmp_path, monkeypatch)
    jewel = tmp_path / ".anneal-memory"
    jewel.mkdir(exist_ok=True)
    (jewel / "m.md").write_text("JEWEL-CONTENT\n")
    ws = tmp_path / "w"
    ws.mkdir()
    swapped = tmp_path / "tmpfile"
    swapped.symlink_to(jewel / "m.md")
    real_rename = os.rename

    def exdev(a, b, *args, **kw):
        if str(a) == str(swapped):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(a, b, *args, **kw)

    monkeypatch.setattr(T.os, "rename", exdev)
    try:
        with pytest.raises((T._FloorRefusedWalk, OSError)):
            E.shutil.move(str(swapped), ws / "out.md")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert not (ws / "out.md").exists()
    assert (jewel / "m.md").read_text() == "JEWEL-CONTENT\n"


def test_a_directory_swapped_for_a_link_mid_walk_is_a_refusal(tmp_path: Path, monkeypatch) -> None:
    """codex closing pass: a component replaced between the walk's stat and its O_NOFOLLOW open
    raised a raw ELOOP past the executor instead of an in-band refusal."""
    import errno

    T, token = _under_floor(tmp_path, monkeypatch)
    (tmp_path / "w" / "d").mkdir(parents=True)
    real_open = os.open

    def racing(path, flags, *args, **kw):
        if path == "d" and kw.get("dir_fd") is not None and flags & os.O_NOFOLLOW:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(T.os, "open", racing)
    try:
        with pytest.raises(T._FloorRefusedWalk):
            T._held_dir(tmp_path / "w" / "d")
    finally:
        T._EDITOR_FLOOR.reset(token)
