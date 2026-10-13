"""The Linux floor's view of the user runtime dir (step (7) of ``_bwrap_plan_impl``).

REPRODUCED 2026-10-12 on argushub (bubblewrap 0.9.0, gnupg 2.4.4) with ``rename_deputy.sh``: on main
72b47da one offline operator-floor session renamed the directory holding a live S.dirmngr and the next
reached it at its new name, 63 packets on port 53 carrying a fresh name; with the view, the directory
is absent inside bash, ``mv`` fails, both probes get ENOENT and the wire count is 0. In the same run
gpg signed through the bound-back agent, ``ssh-add -l`` listed a key through an agent under the runtime
dir, and a socket named in ``runtime_sockets`` answered while it was absent without it
(levain project_memory/1012-dirmngr/).

REPRODUCED the same day for a dirmngr whose socket is in its GNUPGHOME (started where /run/user/<uid>
was absent, still alive when levain ran; ``gnupghome_rename_1012b.sh``): on 0b299c6 renaming the
GNUPGHOME, and renaming its parent, each reached dirmngr from the next session, 60 packets on port 53;
with the GNUPGHOME view both ``mv`` got EBUSY, the probes ENOENT, wire 0. With that view and a
use-keyboxd home, gpg listed and signed through the host's agent and keyboxd (the same pids inside and
out); on 0b299c6 the keyboxd was unreachable inside and gpg started a second one on the same database
(``gpg_view_probe_1012b.sh``).
"""
from __future__ import annotations

import json
import os
import platform
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from levain.firing import confinement as conf
from levain.firing.binding import BindingError, _policy_from_params, _policy_to_params
from levain.firing.confinement import (
    BwrapProvider,
    ConfinementError,
    _bwrap_argv,
    build_policy,
    bwrap_available,
    load_confinement_config,
)

linux_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


def _entity(root: Path) -> Path:
    d = root / "ent"
    (d / ".levain").mkdir(parents=True)
    return d


def _listen(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(8)
    return s


@pytest.fixture
def rd(tmp_path, monkeypatch):
    """A runtime dir with the sockets of argushub's /run/user/1000 in it, as the only runtime dir. Made
    under /tmp because a unix socket path has a short length limit."""
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvrd"))
    socks = [_listen(root / rel) for rel in (
        "gnupg/S.gpg-agent", "gnupg/S.gpg-agent.ssh", "gnupg/S.dirmngr", "gnupg/d.abc/S.dirmngr",
        "systemd/private", "bus", "podman/podman.sock", "ssh-agent.sock")]
    os.symlink(root / "gnupg" / "S.dirmngr", root / "podman" / "link.sock")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(root / "ssh-agent.sock"))
    monkeypatch.setattr(conf, "_runtime_dirs", lambda: [str(root)])
    yield root.resolve()
    for s in socks:
        s.close()
    import shutil
    shutil.rmtree(root)


def _ops(argv: list[str]) -> list[tuple[int, str, list[str]]]:
    """``(index, op, args)`` for every bwrap op after the binary."""
    out, i = [], 1
    while i < len(argv):
        n = conf._BWRAP_OP_ARITY[argv[i]]
        out.append((i, argv[i], argv[i + 1:i + 1 + n]))
        i += 1 + n
    return out


def _under(p: str, root: Path) -> bool:
    return Path(p) != root and Path(p).is_relative_to(root)


def _bound_back(argv: list[str], root: Path) -> set[str]:
    return {a[1] for _, op, a in _ops(argv) if op.endswith("bind") and _under(a[1], root)}


def test_the_runtime_dir_is_a_fresh_tmpfs_after_its_ancestors_pins_and_before_anything_under_it(tmp_path, rd) -> None:
    (rd / "secret").mkdir()
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True,
                                    extra_deny_read_write=(rd / "secret",)))
    ops = _ops(argv)
    view = [i for i, op, a in ops if op == "--tmpfs" and a == [str(rd)]]
    assert len(view) == 1
    assert argv[view[0] - 2:view[0]] == ["--perms", "0700"]   # gnupg refuses a socket dir others can read
    assert ["--tmpfs", str(rd / "secret")] in [argv[k:k + 2] for k in range(len(argv))]
    ancestor_pins = [i for i, op, a in ops if op == "--bind" and rd.is_relative_to(a[1])]
    assert ancestor_pins and max(ancestor_pins) < view[0]   # a later bind of a parent would cover the view
    for i, op, args in ops:
        if args and _under(args[-1], rd):
            assert i > view[0], (op, args)


def test_a_runtime_dir_bash_could_move_from_above_is_pinned_from_above(tmp_path, rd, monkeypatch) -> None:
    """``XDG_RUNTIME_DIR`` two levels under a directory bash can write (``~/.cache/xdg``): renaming its
    parent would carry the host's runtime dir, dirmngr included, to a name the next session has no view
    of. Its ancestors are pinned before its view, as a GNUPGHOME view's are. With the container sockets
    allowed, so that no denied socket under the dir pins its ancestors by another road."""
    top = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvxd"))
    root = top / "a" / "xdg"
    s = _listen(root / "gnupg" / "S.gpg-agent")
    monkeypatch.setattr(conf, "_runtime_dirs", lambda: [str(root)])
    try:
        root = root.resolve()
        argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True,
                                        allow_container_sockets=True))
        ops = _ops(argv)
        view = [i for i, op, a in ops if op == "--tmpfs" and a == [str(root)]]
        assert len(view) == 1
        for anc in (root.parent, root.parent.parent):
            pins = [i for i, op, a in ops if op == "--bind" and a == [str(anc), str(anc)]]
            assert pins and max(pins) < view[0], anc
    finally:
        s.close()
        import shutil
        shutil.rmtree(top)


def test_no_socket_comes_back_on_its_own(tmp_path, rd) -> None:
    """The agents in the runtime dir (gpg's, the ssh agent $SSH_AUTH_SOCK names) are not bound back:
    only an operator grant is, at spawn."""
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    assert _bound_back(argv, rd) == set()
    assert not [a for _, op, a in _ops(argv) if op == "--dir" and _under(a[0], rd)]
    assert not [a for _, op, a in _ops(argv) if op == "--symlink" and _under(a[1], rd)]
    # The deleted per-dir handling: no systemd tmpfs, no bus or dirmngr mask, nothing else under the dir.
    assert not [a for _, op, a in _ops(argv) if op in ("--tmpfs", "--remount-ro") and _under(a[0], rd)]
    assert not [a for _, op, a in _ops(argv) if a[:1] == ["/dev/null"] and _under(a[1], rd)]


@pytest.mark.parametrize("legacy", [["podman/podman.sock"], ["bus", "systemd/private"], "podman/podman.sock"])
def test_a_legacy_runtime_sockets_key_grants_nothing_and_is_reported_ignored(tmp_path, rd, legacy) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": legacy}))
    cfg = load_confinement_config(ent)   # no longer refused: it decides nothing
    assert cfg.runtime_sockets
    assert "runtime_sockets" not in _policy_to_params(build_policy(ent))
    assert _bound_back(_bwrap_argv(build_policy(ent, deny_localhost_outbound=True)), rd) == set()
    lines = conf.unix_socket_lines(cfg, system="Linux")
    assert any(ln.startswith("ignored: runtime_sockets") for ln in lines), lines


def test_chat_refuses_an_entity_whose_shell_gets_an_operator_grant(tmp_path, monkeypatch) -> None:
    """A granted rootless podman can start a container on the host network, the reason chat refuses
    allow_container_sockets; gpg's extra socket cannot, and a hands user's shell gets no grant. The
    post-open check reads the conversation's own snapshot (the one its shell spawns with), so a grant
    added after the session opened is not in it, and neither is it in any respawn of that shell."""
    from types import SimpleNamespace

    from levain import chat as chat_mod

    monkeypatch.setattr(conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {"/run/user/1/gnupg/S.gpg-agent.extra": "/h"})
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text("{}")
    gf = tmp_path / "grants.json"
    monkeypatch.setattr(conf, "grants_file", lambda: gf)
    floor = build_policy(ent, deny_localhost_outbound=True)

    def session_for(hands=None):
        snap = conf.snapshot_operator_grants(hands)
        editor = SimpleNamespace(executor=SimpleNamespace(_policy=floor, _floor=SimpleNamespace(grants=snap)))
        return SimpleNamespace(bash_ok=True, conversation=SimpleNamespace(
            agent=SimpleNamespace(tools_map={"file_editor": editor})))
    podman = [{"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}]
    for grants, refused in (([], False), ([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg",
                                            "why": "signing"}], False), (podman, True)):
        gf.write_text(json.dumps({"grants": grants}))
        gf.chmod(0o600)
        assert ("podman" in (chat_mod.chat_refusal(ent) or "")) is refused
        assert chat_mod._hands_reach_this_server(session_for()) == (
            "may reach the sockets the operator grants" if refused else None)
    assert chat_mod._hands_reach_this_server(session_for(hands=object())) is None
    # Opened with no grant; podman granted afterwards: the session's snapshot (and its shell's) has none.
    gf.write_text(json.dumps({"grants": []}))
    opened = session_for()
    gf.write_text(json.dumps({"grants": podman}))
    assert opened.conversation.agent.tools_map["file_editor"].executor._floor.grants.get() == ()
    assert chat_mod._hands_reach_this_server(opened) is None
    # On macOS no grant is bound, so chat cites none.
    monkeypatch.setattr(conf.platform, "system", lambda: "Darwin")
    assert chat_mod.chat_refusal(ent) is None
    assert chat_mod._hands_reach_this_server(session_for()) is None


@pytest.mark.parametrize("held", ["workspace", "entity", "home"])
def test_a_runtime_dir_holding_the_workspace_the_entity_or_home_is_refused(tmp_path, rd, monkeypatch, held) -> None:
    """``XDG_RUNTIME_DIR`` pointed at a directory of the operator's: its view would hide what it holds."""
    ent = _entity(rd / "e" if held == "entity" else tmp_path)
    ws = rd / "ws" if held == "workspace" else tmp_path / "ws"
    ws.mkdir()
    if held == "home":
        (rd / "home").mkdir()
        monkeypatch.setenv("HOME", str(rd / "home"))
    with pytest.raises(ConfinementError, match="runtime dir"):
        _bwrap_argv(build_policy(ent, workspace=ws, deny_localhost_outbound=True))


def test_a_socket_outside_the_runtime_dir_is_unaffected(tmp_path, rd, monkeypatch) -> None:
    other = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvssh"))
    s = _listen(other / "agent.sock")
    try:
        monkeypatch.setenv("SSH_AUTH_SOCK", str(other / "agent.sock"))
        argv = _bwrap_argv(build_policy(_entity(tmp_path)))
        assert not [a for _, op, a in _ops(argv) if a and a[-1].startswith(str(other.resolve()))]
        assert str(rd / "ssh-agent.sock") not in _bound_back(argv, rd)
    finally:
        s.close()
        (other / "agent.sock").unlink()
        other.rmdir()


@linux_live
def test_linux_live_a_renamed_dirmngr_dir_is_not_reachable_and_the_agent_is(tmp_path, rd) -> None:
    """The 2026-10-10 attack against the view, with a stand-in agent that answers OK."""
    agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    agent.bind(str(rd / "gnupg" / "S.gpg-agent.live"))
    agent.listen(4)
    agent.settimeout(20)

    def serve() -> None:
        try:
            c, _ = agent.accept()
            c.sendall(b"OK agent\n")
            c.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    gf = tmp_path / "grants.json"
    gf.write_text(json.dumps({"grants": [{"path": str(rd / "gnupg" / "S.gpg-agent.live"), "name": "agent",
                                          "why": "the stand-in agent"}]}))
    gf.chmod(0o600)
    connect = ("python3 -c \"import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(5);"
               "s.connect(sys.argv[1]);print(s.recv(64))\" ")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conf, "grants_file", lambda: gf)
            mp.delenv("SSH_AUTH_SOCK")
            with BwrapProvider().spawn_shell(build_policy(_entity(tmp_path), deny_localhost_outbound=True),
                                             grants=conf.load_operator_grants()) as sh:
                assert sh.run(f"ls -A {rd}").output.split() == ["gnupg"]
                assert sh.run(f"ls -A {rd}/gnupg").output.split() == ["S.gpg-agent.live"]
                assert sh.run(f"mv {rd}/gnupg/d.abc {rd}/moved").exit_code != 0
                assert sh.run(connect + f"{rd}/moved/S.dirmngr").exit_code != 0
                assert sh.run(connect + f"{rd}/gnupg/d.abc/S.dirmngr").exit_code != 0
                assert sh.run(f"ls {rd}/systemd {rd}/bus").exit_code != 0
                r = sh.run(connect + f"{rd}/gnupg/S.gpg-agent.live")
                assert r.exit_code == 0 and "OK agent" in r.output, r.output
    finally:
        agent.close()
    assert (rd / "gnupg" / "d.abc" / "S.dirmngr").exists() and not (rd / "moved").exists()


def test_bash_gpg_finds_its_agent_where_gpgconf_says_for_the_shells_own_home(monkeypatch) -> None:
    """``gpgconf --list-dirs agent-socket`` with the shell env's GNUPGHOME (none: the default home) and
    its HOME, never levain's own GNUPGHOME; the value percent-unescaped."""
    from types import SimpleNamespace

    seen = []
    monkeypatch.setenv("GNUPGHOME", "/levains/own")
    monkeypatch.setattr(conf.shutil, "which", lambda name: "/usr/bin/gpgconf")
    monkeypatch.setattr(conf.subprocess, "run", lambda argv, **kw: seen.append((argv, kw["env"])) or SimpleNamespace(
        returncode=0, stdout="/run/user/1/gnupg/d.x%3ay/S.gpg-agent\n"))
    assert conf.gpg_agent_view_path({"HOME": "/home/u"}) == Path("/run/user/1/gnupg/d.x:y/S.gpg-agent")
    assert seen[0][0] == ["/usr/bin/gpgconf", "--list-dirs", "agent-socket"]
    assert seen[0][1]["HOME"] == "/home/u" and "GNUPGHOME" not in seen[0][1]
    conf.gpg_agent_view_path({"HOME": "/home/u", "GNUPGHOME": "/home/u/g"})
    assert seen[1][0] == ["/usr/bin/gpgconf", "--homedir", "/home/u/g", "--list-dirs", "agent-socket"]
    assert "GNUPGHOME" not in seen[1][1]


def test_keyboxd_does_not_come_back_into_the_runtime_view(tmp_path, rd) -> None:
    kb = _listen(rd / "gnupg" / "S.keyboxd")
    try:
        bound = _bound_back(_bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True)), rd)
        assert bound == set()
    finally:
        kb.close()


@pytest.fixture
def gh(rd, monkeypatch):
    """A GNUPGHOME outside the runtime dir holding a live dirmngr, laid out as gnupg leaves one that
    started with no /run/user/<uid>: two levels below a fresh /tmp dir, so it and its parent are
    renameable."""
    top = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvgh"))
    g = top / "a" / "gh"
    g.mkdir(parents=True, mode=0o700)
    socks = [_listen(g / n) for n in ("S.dirmngr", "S.gpg-agent", "S.gpg-agent.extra", "S.keyboxd")]
    (g / "trustdb.gpg").write_bytes(b"trust")
    (g / "common.conf").write_text("use-keyboxd\n")
    (g / "private-keys-v1.d").mkdir()
    os.symlink("/nonexistent/gpg.conf", g / "gpg.conf")
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [g / "S.dirmngr"])
    yield g.resolve()
    for s in socks:
        s.close()
    import shutil
    shutil.rmtree(top)


def test_a_gnupghome_holding_dirmngr_is_a_view_pinned_from_above(tmp_path, gh) -> None:
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    ops = _ops(argv)
    view = [i for i, op, a in ops if op == "--tmpfs" and a == [str(gh)]]
    assert len(view) == 1 and argv[view[0] - 2:view[0]] == ["--perms", "0700"]
    for anc in (gh.parent, gh.parent.parent):   # renaming either moved dirmngr past the mask (RUN)
        pins = [i for i, op, a in ops if op == "--bind" and a == [str(anc), str(anc)]]
        assert pins and max(pins) < view[0], anc
    for i, op, args in ops:
        if args and _under(args[-1], gh):
            assert i > view[0], (op, args)
    assert _bound_back(argv, gh) == {str(gh / n) for n in ("trustdb.gpg", "common.conf")}
    # Writable: with the trustdb read-only, `gpg --list-keys` died on its trustdb check (RUN 2026-10-12).
    assert ["--bind", str(gh / "trustdb.gpg"), str(gh / "trustdb.gpg")] in [argv[k:k + 3] for k in range(len(argv))]
    # A configuration file comes back read-only, and a link to none stays out (Diogenes HIGH 2026-10-13).
    assert ["--ro-bind", str(gh / "common.conf"), str(gh / "common.conf")] in [argv[k:k + 3] for k in range(len(argv))]
    assert not [a for _, op, a in ops if a and a[-1] == str(gh / "gpg.conf")]
    # nor is its target, which no one here can create: no mask and no pin of /nonexistent (RUN, CI bwrap job)
    assert not [a for _, op, a in ops if any(x.startswith("/nonexistent") for x in a)]
    assert not [a for _, op, a in ops if a[:1] == ["/dev/null"] and _under(a[1], gh)]   # the view replaces the mask


def test_dirmngr_sockets_are_read_once_per_plan(tmp_path, gh, monkeypatch) -> None:
    """Read twice, a dirmngr that appeared between the view and the masks got a mask only, which a
    rename moves past (complement L3 r1)."""
    calls = []
    policy = build_policy(_entity(tmp_path), deny_localhost_outbound=True)   # its own reading: the editor's
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: calls.append(1) or [gh / "S.dirmngr"])
    argv = _bwrap_argv(policy)
    assert calls == [1]
    assert [i for i, op, a in _ops(argv) if op == "--tmpfs" and a == [str(gh)]]


def test_a_gnupghome_that_vanished_before_its_stat_is_skipped(tmp_path, gh, monkeypatch) -> None:
    """The dirmngr and its home went away between reading the socket list and the stat: nothing is left
    to hide, so no view, and no traceback out of the floor."""
    real_stat = os.stat

    def gone(p, *a, **k):
        if Path(p) == gh:
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(conf.os, "stat", gone)
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    assert not [a for _, op, a in _ops(argv) if op == "--tmpfs" and a == [str(gh)]]


def test_a_gnupghome_view_binds_back_no_socket(tmp_path, gh) -> None:
    extra = [_listen(gh / n) for n in ("S.other", "dirmngr.sock", "S.gpg-agent.browser")]
    try:
        argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
        socks = {p for p in _bound_back(argv, gh) if Path(p).is_socket()}
        assert socks == set()
    finally:
        for s in extra:
            s.close()


def test_a_gnupghome_that_cannot_be_listed_is_refused_naming_it(tmp_path, gh) -> None:
    gh.chmod(0o300)
    try:
        with pytest.raises(ConfinementError, match="could not list"):
            _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    finally:
        gh.chmod(0o700)


def test_a_gnupghome_owned_by_another_user_gets_no_view(tmp_path, gh, monkeypatch) -> None:
    """Its files are not the operator's to bind back into a view: its dirmngr keeps the mask."""
    real_stat = os.stat
    monkeypatch.setattr(conf.os, "stat", lambda p, *a, **k: (
        os.stat_result((*real_stat(p, *a, **k)[:4], os.geteuid() + 1, *real_stat(p, *a, **k)[5:]))
        if Path(p) == gh else real_stat(p, *a, **k)))
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    assert not [a for _, op, a in _ops(argv) if op == "--tmpfs" and a == [str(gh)]]
    # Nothing of the home is bound back; the one bind under it is the dirmngr's /dev/null mask, which on
    # Linux is spelled as gh is (on macOS /tmp against /private/tmp, so it never collided).
    assert not [a for _, op, a in _ops(argv) if op in conf._MASK_OPS + ("--dev-bind", "--dev-bind-try") and _under(a[1], gh) and a[0] != "/dev/null"]
    assert [a for _, op, a in _ops(argv) if a[:1] == ["/dev/null"] and Path(a[1]).resolve() == gh / "S.dirmngr"]


@pytest.fixture
def home_gh(rd, monkeypatch):
    """A GNUPGHOME holding a live dirmngr two levels under $HOME (``~/x/gh``), so step (0)'s view of
    $HOME, the pins of ``~/x`` and the GNUPGHOME's own view all meet. $HOME is made under /tmp because
    a unix socket path has a short length limit."""
    home = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvhm")).resolve()
    monkeypatch.setenv("HOME", str(home))
    g = home / "x" / "gh"
    g.mkdir(parents=True, mode=0o700)
    (home / "x" / "notes").write_text("n")
    socks = [_listen(g / n) for n in ("S.dirmngr", "S.gpg-agent")]
    (g / "trustdb.gpg").write_bytes(b"trust")
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [g / "S.dirmngr"])
    yield g
    for s in socks:
        s.close()
    import shutil
    shutil.rmtree(home)


def test_a_gnupghome_under_home_is_a_view_nothing_later_covers(tmp_path, home_gh) -> None:
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    ops = _ops(argv)
    view = [i for i, op, a in ops if op == "--tmpfs" and a == [str(home_gh)]]
    assert len(view) == 1
    covering = {str(home_gh), *(str(a) for a in home_gh.parents)}
    # After the view, no op of any kind lands on it or on a directory above it: a mount there would
    # cover the view and put the host's GNUPGHOME, dirmngr included, back in front of bash. bwrap's
    # --remount-ro is not recursive, so $HOME's own leaves the view below it as it is.
    later = [(op, a) for i, op, a in ops if i > view[0] and a and a[-1] in covering and op != "--remount-ro"]
    assert later == []
    assert _bound_back(argv, home_gh) == {str(home_gh / "trustdb.gpg")}
    pins = [i for i, op, a in ops if op == "--bind" and a == [str(home_gh.parent)] * 2]
    assert pins and max(pins) < view[0]


@pytest.mark.parametrize("tool", [".kube", ".docker", ".aws", ".config/git"])
def test_a_gnupghome_under_a_tool_directory_view_is_refused(tmp_path, rd, monkeypatch, tool) -> None:
    """A tool directory's view is a read-only tmpfs placed after the GNUPGHOME's, so it would cover
    that view, and bind the GNUPGHOME back from the host as one of its subdirectories."""
    home = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvhm")).resolve()
    monkeypatch.setenv("HOME", str(home))
    g = home / tool / "gh"
    g.mkdir(parents=True, mode=0o700)
    s = _listen(g / "S.dirmngr")
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [g / "S.dirmngr"])
    try:
        with pytest.raises(ConfinementError, match="tool directory"):
            _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True, deny_standard_creds=True))
    finally:
        s.close()
        import shutil
        shutil.rmtree(home)


def test_a_gnupghome_gets_no_view_when_bash_has_network_but_its_configuration_stays_read_only(tmp_path, gh) -> None:
    """dirmngr is a deputy only for bash without network; with network bash fetches by itself, so no view.
    The agent beside that dirmngr still reads the configuration files, so they stay read-only either way
    (Diogenes HIGH 2026-10-13)."""
    (gh / "gpg.conf").unlink()   # a dangling link where no view takes it is refused, as in ~/.gnupg since 0.9.1
    policy = build_policy(_entity(tmp_path))
    argv = _bwrap_argv(policy)
    touched = [(op, a) for _, op, a in _ops(argv) if a and (_under(a[-1], gh) or a[-1] == str(gh))]
    assert touched and all(op == "--ro-bind" and a[-1].endswith(".conf") or a == [str(gh), str(gh)]
                           for op, a in touched), touched
    assert ("--ro-bind", [str(gh / "common.conf"), str(gh / "common.conf")]) in touched
    assert conf.crown_jewel_reason(policy, gh / "gpg-agent.conf") is not None   # the file editor too


def test_a_gnupghome_holding_the_workspace_is_refused(tmp_path, gh) -> None:
    ent = _entity(tmp_path)
    with pytest.raises(ConfinementError, match="holds"):
        (gh / "ws").mkdir()
        _bwrap_argv(build_policy(ent, workspace=gh / "ws", deny_localhost_outbound=True))


def test_a_dirmngr_directly_in_home_keeps_its_mask(tmp_path, rd, monkeypatch) -> None:
    """$HOME's own entries are frozen by its view, so bash cannot rename the socket away: the mask holds."""
    home = Path(os.environ["HOME"]).resolve()
    (home / "S.dirmngr").write_text("")
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [home / "S.dirmngr"])
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    assert ["--ro-bind", "/dev/null", str(home / "S.dirmngr")] in [argv[k:k + 3] for k in range(len(argv))]
    assert ["--perms", "0700", "--tmpfs", str(home)] not in [argv[k:k + 4] for k in range(len(argv))]


@pytest.mark.parametrize("knows_perms", [True, False])
def test_a_bwrap_without_perms_reads_unavailable_naming_the_minimum(tmp_path, monkeypatch, knows_perms) -> None:
    """Every runtime view is made with ``--perms``, which bubblewrap has from 0.5.0. A 0.4.x (Ubuntu
    20.04, Debian 11) passed the probe and every shell then died "Unknown option --perms"."""
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n" + ("" if knows_perms else
                    'for a in "$@"; do [ "$a" = --perms ] && { echo "bwrap: Unknown option --perms" >&2; exit 1; }; done\n')
                    + "exit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(conf, "BWRAP", str(fake))
    monkeypatch.setattr(conf, "_cgroup_problem", lambda: None)
    monkeypatch.setattr(conf, "_leaf_chain_problem", lambda: None)
    assert bwrap_available() is knows_perms
    d = conf.diagnose_confinement("Linux")
    assert d.supported is knows_perms
    if not knows_perms:
        assert "0.5.0" in d.reason and "--perms" in d.reason, d.reason


def test_gnupghome_passes_the_launch_allowlist() -> None:
    """Under ``levain run`` the re-exec keeps only allowlisted names, and gpgconf needs GNUPGHOME to
    name a non-default home's sockets."""
    from levain.launch import allowed

    assert allowed("GNUPGHOME")


@linux_live
def test_linux_live_a_gnupghome_view_hides_dirmngr_and_the_agent(tmp_path, rd, gh) -> None:
    """The 2026-10-12 GNUPGHOME attack against the view, with stand-in sockets that answer. The agent is
    not bound back either (only an operator grant would be)."""
    for s in ("S.dirmngr", "S.gpg-agent"):
        (gh / s).unlink()
    answer = {}
    for s in ("S.dirmngr", "S.gpg-agent"):
        srv = _listen(gh / s)
        srv.settimeout(20)
        answer[s] = srv

        def serve(srv=srv, name=s) -> None:
            try:
                c, _ = srv.accept()
                c.sendall(f"OK {name}\n".encode())
                c.close()
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
    connect = ("python3 -c \"import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(5);"
               "s.connect(sys.argv[1]);print(s.recv(64))\" ")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("SSH_AUTH_SOCK")
            with BwrapProvider().spawn_shell(build_policy(_entity(tmp_path), deny_localhost_outbound=True)) as sh:
                listed = sh.run(f"ls -A {gh}").output.split()
                assert sorted(listed) == ["common.conf", "trustdb.gpg"]
                assert sh.run(f"echo pinentry-program /bin/true >> {gh}/common.conf").exit_code != 0
                assert sh.run(connect + f"{gh}/S.dirmngr").exit_code != 0
                assert sh.run(f"mv {gh} {gh}.moved").exit_code != 0
                assert sh.run(f"mv {gh.parent} {gh.parent}.moved").exit_code != 0
                assert sh.run(f"echo x >> {gh}/trustdb.gpg").exit_code == 0
                assert sh.run(connect + f"{gh}/S.gpg-agent").exit_code != 0
    finally:
        for srv in answer.values():
            srv.close()
    assert (gh / "S.dirmngr").exists() and not Path(f"{gh}.moved").exists()
    assert (gh / "trustdb.gpg").read_bytes() == b"trustx\n"
    assert (gh / "common.conf").read_text() == "use-keyboxd\n"   # the host file, unchanged


def test_a_gnupghome_view_keeps_gnupgs_configuration_files_read_only(tmp_path, gh, monkeypatch) -> None:
    """The 0.9.1 fix makes gnupg's configuration files read-only to bash in $GNUPGHOME. A GNUPGHOME view
    is placed after those binds, so its tmpfs covers them: a file it binds back writable would undo
    that fix for the one home the view exists for."""
    monkeypatch.setenv("GNUPGHOME", str(gh))
    (gh / "gpg.conf").unlink()   # a dangling link there is refused by 0.9.1's floor before any view
    (gh / "gpg-agent.conf").write_text("")
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    ops = _ops(argv)
    view = max(i for i, op, a in ops if op == "--tmpfs" and a == [str(gh)])
    for name in ("gpg-agent.conf", "common.conf"):
        last = [(i, op) for i, op, a in ops if a and a[-1] == str(gh / name)]
        assert last and last[-1][0] > view and last[-1][1] == "--ro-bind", (name, last)
    # trustdb stays writable (RUN 2026-10-12: read-only, `gpg --list-keys` died on its trustdb check)
    assert ["--bind", str(gh / "trustdb.gpg"), str(gh / "trustdb.gpg")] in [argv[k:k + 3] for k in range(len(argv))]


def test_a_gnupghome_view_levain_does_not_name_keeps_its_configuration_files_read_only(tmp_path, gh, monkeypatch) -> None:
    """The view exists for a home levain's environment does NOT name (a cron or pre-linger gpg), so no
    0.9.1 bind covers it: the view itself must bring the configuration files back read-only, a link as
    its target's content. REPRODUCED by Diogenes 2026-10-13 on 977d5bd: the test above minus its
    GNUPGHOME setenv failed ``('gpg-agent.conf', [(True, '--bind')])`` (shipped in 0.10.0 and 0.10.1)."""
    monkeypatch.delenv("GNUPGHOME", raising=False)
    (gh / "gpg.conf").unlink()
    (gh / "gpg-agent.conf").write_text("")
    target = gh.parent / "dirmngr.conf.real"
    target.write_text("")
    os.symlink(target, gh / "dirmngr.conf")   # a dotfile-manager link: writing through it reached the host
    os.symlink(gh.parent / "absent.conf", gh / "scdaemon.conf")   # leads nowhere: stays out
    (gh / "agent.local").write_text("")
    (gh / "gpg-agent.conf").unlink()
    os.symlink("agent.local", gh / "gpg-agent.conf")   # a sibling target must not stay writable (L2)
    pol = build_policy(_entity(tmp_path), deny_localhost_outbound=True)
    argv = _bwrap_argv(pol)
    ops = _ops(argv)
    view = max(i for i, op, a in ops if op == "--tmpfs" and a == [str(gh)])
    for name in ("gpg-agent.conf", "common.conf", "dirmngr.conf"):
        last = [(i, op) for i, op, a in ops if a and a[-1] == str(gh / name)]
        assert last and last[-1][0] > view and last[-1][1] == "--ro-bind", (name, last)
    assert ["--ro-bind", str(target.resolve()), str(gh / "dirmngr.conf")] in [argv[k:k + 3] for k in range(len(argv))]
    last = [op for i, op, a in ops if a and a[-1] == str(gh / "agent.local")]
    assert last and last[-1] == "--ro-bind", last
    for name in ("gpg-agent.conf", "dirmngr.conf", "common.conf"):   # the file editor refuses them too
        assert conf.crown_jewel_reason(pol, gh / name) is not None, name
    assert conf.crown_jewel_reason(pol, target) is not None   # and a link's target outside the home
    assert not [a for _, op, a in ops if a and a[-1] == str(gh / "scdaemon.conf")]
    assert ["--bind", str(gh / "trustdb.gpg"), str(gh / "trustdb.gpg")] in [argv[k:k + 3] for k in range(len(argv))]
    # L3 r1: a dirmngr home that appears after the policy was built reaches the editor at the next spawn
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [])
    early = build_policy(_entity(tmp_path / "e2"), deny_localhost_outbound=True)
    assert conf.crown_jewel_reason(early, gh / "gpg-agent.conf") is None
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: [gh / "S.dirmngr"])
    assert conf.crown_jewel_reason(conf.refresh_socket_denies(early), gh / "gpg-agent.conf") is not None


def test_a_gnupghome_view_carries_no_jewel_in_and_drops_no_other_deny(tmp_path, gh, monkeypatch) -> None:
    """L3 r1 (codex + complement): a configuration link into a denied subtree must not bring the jewel's
    content into the view, and a write-denied file of another kind in the home keeps its read-only bind."""
    jewel = tmp_path / "home" / ".anneal-memory"
    jewel.mkdir(parents=True)
    (jewel / "secret").write_text("x")
    (gh / "gpg-agent.conf").unlink(missing_ok=True)
    os.symlink(jewel / "secret", gh / "gpg-agent.conf")
    (gh / "trust.json").write_text("{}")
    monkeypatch.setenv("ANNEAL_MEMORY_DERIVE_TRUST", str(gh / "trust.json"))
    ops = _ops(_bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True)))
    assert not [a for _, op, a in ops if a and a[-1] == str(gh / "gpg-agent.conf")]
    assert not [a for _, op, a in ops if a and a[0] == str((jewel / "secret").resolve()) and _under(a[-1], gh)]
    last = [op for _, op, a in ops if a and a[-1] == str(gh / "trust.json")]
    assert last and last[-1] == "--ro-bind", last


def test_a_tmpfs_refusal_that_does_not_name_perms_is_not_read_as_an_old_bwrap(tmp_path, monkeypatch) -> None:
    """A current bubblewrap whose tmpfs mount an LSM refuses fails the --perms probe too, but its error
    names the mount, not the option: the doctor must not tell that operator to upgrade."""
    fake = tmp_path / "bwrap"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = --tmpfs ] && { echo "bwrap: Failed to mount tmpfs: '
                    'Permission denied" >&2; exit 1; }; done\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setattr(conf, "BWRAP", str(fake))
    assert conf._bwrap_lacks_perms() is False
