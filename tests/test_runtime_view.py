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
    monkeypatch.setattr(conf, "_gpg_sockets",
                        lambda: [root / "gnupg" / "S.gpg-agent", root / "gnupg" / "S.gpg-agent.ssh"])
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


def test_only_socket_files_come_back_never_a_directory(tmp_path, rd) -> None:
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
    for _, op, args in _ops(argv):
        if op.endswith("bind") and _under(args[1], rd):
            assert args[0] == args[1] and not os.path.isdir(args[0]), (op, args)
    assert _bound_back(argv, rd) == {str(rd / "gnupg" / "S.gpg-agent"), str(rd / "gnupg" / "S.gpg-agent.ssh"),
                                     str(rd / "ssh-agent.sock")}
    assert ["--perms", "0700", "--dir", str(rd / "gnupg")] in [argv[k:k + 4] for k in range(len(argv))]
    # The deleted per-dir handling: no systemd tmpfs, no bus or dirmngr mask, nothing else under the dir.
    assert not [a for _, op, a in _ops(argv) if op in ("--tmpfs", "--remount-ro") and _under(a[0], rd)]
    assert not [a for _, op, a in _ops(argv) if a[:1] == ["/dev/null"] and _under(a[1], rd)]


def test_an_allowlisted_socket_comes_back_and_a_link_to_a_refused_one_does_not(tmp_path, rd) -> None:
    (rd / "podman" / "file").write_text("not a socket")
    policy = build_policy(_entity(tmp_path), runtime_sockets=(
        "podman/podman.sock", "podman/link.sock", "podman/file", "gnupg/d.abc", "podman/absent.sock"))
    bound = _bound_back(_bwrap_argv(policy), rd)
    assert bound - {str(rd / n) for n in ("gnupg/S.gpg-agent", "gnupg/S.gpg-agent.ssh", "ssh-agent.sock")} == {
        str(rd / "podman" / "podman.sock")}
    # absent by default, though it is on the container-socket roster
    assert str(rd / "podman" / "podman.sock") not in _bound_back(_bwrap_argv(build_policy(_entity(tmp_path / "b"))), rd)


def test_a_name_that_is_a_link_comes_back_as_the_link_and_a_link_to_a_refused_socket_does_not(tmp_path, rd, monkeypatch) -> None:
    """Only the resolved socket was bound back, so ``SSH_AUTH_SOCK=<rd>/agent-link`` (a link to the
    agent's ssh socket) and an allowlisted link both named nothing inside bash."""
    os.symlink("gnupg/S.gpg-agent.ssh", rd / "agent-link")
    os.symlink("podman/podman.sock", rd / "docker.sock")
    (rd / "sub").mkdir()
    os.symlink("../docker.sock", rd / "sub" / "chain.sock")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(rd / "agent-link"))
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True, runtime_sockets=(
        "docker.sock", "sub/chain.sock", "podman/link.sock")))
    windows = [argv[k:k + 3] for k in range(len(argv))]
    assert ["--symlink", "gnupg/S.gpg-agent.ssh", str(rd / "agent-link")] in windows
    assert ["--symlink", "podman/podman.sock", str(rd / "docker.sock")] in windows
    # a chain comes back as one link, to the socket itself
    assert ["--symlink", str(rd / "podman" / "podman.sock"), str(rd / "sub" / "chain.sock")] in windows
    assert ["--perms", "0700", "--dir", str(rd / "sub")] in [argv[k:k + 4] for k in range(len(argv))]
    assert _bound_back(argv, rd) >= {str(rd / "gnupg" / "S.gpg-agent.ssh"), str(rd / "podman" / "podman.sock")}
    ops = _ops(argv)
    view = max(i for i, op, a in ops if op == "--tmpfs" and a == [str(rd)])
    links = [i for i, op, a in ops if op == "--symlink" and _under(a[1], rd)]
    assert links and min(links) > view
    # podman/link.sock -> gnupg/S.dirmngr: neither the link nor what it resolves to comes back
    for _, op, a in ops:
        assert str(rd / "podman" / "link.sock") not in a and str(rd / "gnupg" / "S.dirmngr") not in a, (op, a)


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


@pytest.mark.parametrize("entry,why", [
    ("systemd/private", "service manager"), ("systemd/x/y", "service manager"), ("bus", "session bus"),
    ("gnupg/S.dirmngr", "dirmngr"), ("gnupg/d.abc/S.dirmngr", "dirmngr"), ("../run/x", "inside the runtime dir"),
    ("/run/user/1000/podman/podman.sock", "inside the runtime dir"), ("", "inside the runtime dir"),
    (".", "inside the runtime dir"), ("./", "inside the runtime dir"), ("podman/\0x.sock", "NUL"),
])
def test_runtime_sockets_refuses_the_service_manager_the_bus_and_dirmngr(tmp_path, entry, why) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": [entry]}))
    with pytest.raises(ConfinementError, match=why):
        load_confinement_config(ent)
    with pytest.raises(ConfinementError, match=why):
        build_policy(ent, runtime_sockets=(entry,))


def test_runtime_sockets_loads_and_survives_the_binding(tmp_path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": ["podman/podman.sock"]}))
    assert load_confinement_config(ent).runtime_sockets == ("podman/podman.sock",)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": "podman/podman.sock"}))
    with pytest.raises(ConfinementError, match="runtime_sockets"):
        load_confinement_config(ent)
    params = _policy_to_params(build_policy(ent, runtime_sockets=("podman/podman.sock",)))
    assert _policy_from_params(params).runtime_sockets == ("podman/podman.sock",)
    with pytest.raises(BindingError, match="session bus"):
        _policy_from_params({**params, "runtime_sockets": ["bus"]})


def test_chat_refuses_an_entity_whose_shell_gets_runtime_sockets(tmp_path) -> None:
    """A rootless podman bound back can start a container on the host network, the reason chat refuses
    allow_container_sockets."""
    from types import SimpleNamespace

    from levain import chat as chat_mod

    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": ["podman/podman.sock"]}))
    assert "runtime_sockets" in (chat_mod.chat_refusal(ent) or "")
    (ent / ".levain" / "confinement.json").write_text("{}")
    assert chat_mod.chat_refusal(ent) is None
    for listed, reach in ((("podman/podman.sock",), "may reach the daemon sockets its runtime_sockets lists"),
                          ((), None)):
        floor = build_policy(ent, deny_localhost_outbound=True, runtime_sockets=listed)
        editor = SimpleNamespace(executor=SimpleNamespace(_policy=floor))
        session = SimpleNamespace(bash_ok=True, conversation=SimpleNamespace(
            agent=SimpleNamespace(tools_map={"file_editor": editor})))
        assert chat_mod._hands_reach_this_server(session) == reach


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
    conf_sockets = [rd / "gnupg" / "S.gpg-agent.live"]
    connect = ("python3 -c \"import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(5);"
               "s.connect(sys.argv[1]);print(s.recv(64))\" ")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conf, "_gpg_sockets", lambda: conf_sockets)
            mp.delenv("SSH_AUTH_SOCK")
            with BwrapProvider().spawn_shell(build_policy(_entity(tmp_path), deny_localhost_outbound=True)) as sh:
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


def test_gpg_sockets_reads_all_of_list_dirs_and_keeps_the_agent_and_keyboxd(monkeypatch) -> None:
    """One ``gpgconf --list-dirs`` with no names (gnupg 2.2 fails a query naming keyboxd-socket), its
    values percent-unescaped, and only the agent's two sockets and keyboxd's picked out."""
    from types import SimpleNamespace

    out = ("sysconfdir:/etc/gnupg\nhomedir:/home/u/g%3ah\nagent-socket:/run/user/1/gnupg/d.x/S.gpg-agent\n"
           "agent-ssh-socket:/run/user/1/gnupg/d.x/S.gpg-agent.ssh\n"
           "agent-extra-socket:/run/user/1/gnupg/d.x/S.gpg-agent.extra\n"
           "agent-browser-socket:/run/user/1/gnupg/d.x/S.gpg-agent.browser\n"
           "dirmngr-socket:/run/user/1/gnupg/d.x/S.dirmngr\n"
           "keyboxd-socket:/run/user/1/gnupg/d%3ax/S.keyboxd\n")
    seen = []
    monkeypatch.setattr(conf.shutil, "which", lambda name: "/usr/bin/gpgconf")
    monkeypatch.setattr(conf.subprocess, "run", lambda argv, **kw: seen.append(argv) or SimpleNamespace(
        returncode=0, stdout=out))
    assert conf._gpg_sockets() == [Path("/run/user/1/gnupg/d.x/S.gpg-agent"),
                                   Path("/run/user/1/gnupg/d.x/S.gpg-agent.ssh"),
                                   Path("/run/user/1/gnupg/d:x/S.keyboxd")]
    assert seen == [["/usr/bin/gpgconf", "--list-dirs"]]


def test_keyboxd_comes_back_into_the_runtime_view(tmp_path, rd, monkeypatch) -> None:
    kb = _listen(rd / "gnupg" / "S.keyboxd")
    try:
        monkeypatch.setattr(conf, "_gpg_sockets", lambda: [rd / "gnupg" / "S.gpg-agent", rd / "gnupg" / "S.keyboxd"])
        bound = _bound_back(_bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True)), rd)
        assert str(rd / "gnupg" / "S.keyboxd") in bound
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
    assert _bound_back(argv, gh) == {str(gh / n) for n in ("S.gpg-agent", "S.keyboxd", "trustdb.gpg", "common.conf")}
    # Writable: with the trustdb read-only, `gpg --list-keys` died on its trustdb check (RUN 2026-10-12).
    assert ["--bind", str(gh / "trustdb.gpg"), str(gh / "trustdb.gpg")] in [argv[k:k + 3] for k in range(len(argv))]
    assert ["--symlink", "/nonexistent/gpg.conf", str(gh / "gpg.conf")] in [argv[k:k + 3] for k in range(len(argv))]
    assert not [a for _, op, a in ops if a[:1] == ["/dev/null"] and _under(a[1], gh)]   # the view replaces the mask


def test_dirmngr_sockets_are_read_once_per_plan(tmp_path, gh, monkeypatch) -> None:
    """Read twice, a dirmngr that appeared between the view and the masks got a mask only, which a
    rename moves past (complement L3 r1)."""
    calls = []
    monkeypatch.setattr(conf, "_dirmngr_sockets", lambda: calls.append(1) or [gh / "S.dirmngr"])
    argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
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


def test_a_gnupghome_view_binds_back_no_socket_but_the_agent_and_keyboxd(tmp_path, gh) -> None:
    extra = [_listen(gh / n) for n in ("S.other", "dirmngr.sock", "S.gpg-agent.browser")]
    try:
        argv = _bwrap_argv(build_policy(_entity(tmp_path), deny_localhost_outbound=True))
        socks = {p for p in _bound_back(argv, gh) if Path(p).is_socket()}
        assert socks == {str(gh / "S.gpg-agent"), str(gh / "S.keyboxd")}
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
    assert not _bound_back(argv, gh)
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
    assert _bound_back(argv, home_gh) == {str(home_gh / "S.gpg-agent"), str(home_gh / "trustdb.gpg")}
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


def test_a_gnupghome_is_left_alone_when_bash_has_network(tmp_path, gh) -> None:
    """dirmngr is a deputy only for bash without network; with network bash fetches by itself."""
    argv = _bwrap_argv(build_policy(_entity(tmp_path)))
    assert not [a for _, op, a in _ops(argv) if a and (_under(a[-1], gh) or a[-1] == str(gh))]


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
def test_linux_live_a_gnupghome_view_hides_dirmngr_and_keeps_the_agent(tmp_path, rd, gh) -> None:
    """The 2026-10-12 GNUPGHOME attack against the view, with stand-in sockets that answer."""
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
                assert sorted(listed) == ["S.gpg-agent", "S.keyboxd", "common.conf", "gpg.conf", "trustdb.gpg"]
                assert sh.run(connect + f"{gh}/S.dirmngr").exit_code != 0
                assert sh.run(f"mv {gh} {gh}.moved").exit_code != 0
                assert sh.run(f"mv {gh.parent} {gh.parent}.moved").exit_code != 0
                assert sh.run(f"echo x >> {gh}/trustdb.gpg").exit_code == 0
                r = sh.run(connect + f"{gh}/S.gpg-agent")
                assert r.exit_code == 0 and "OK S.gpg-agent" in r.output, r.output
    finally:
        for srv in answer.values():
            srv.close()
    assert (gh / "S.dirmngr").exists() and not Path(f"{gh}.moved").exists()
    assert (gh / "trustdb.gpg").read_bytes() == b"trustx\n"


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
