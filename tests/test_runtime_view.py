"""The Linux floor's view of the user runtime dir (step (7) of ``_bwrap_plan_impl``).

REPRODUCED 2026-10-12 on argushub (bubblewrap 0.9.0, gnupg 2.4.4) with ``rename_deputy.sh``: on main
72b47da one offline operator-floor session renamed the directory holding a live S.dirmngr and the next
reached it at its new name, 63 packets on port 53 carrying a fresh name; with the view, the directory
is absent inside bash, ``mv`` fails, both probes get ENOENT and the wire count is 0. In the same run
gpg signed through the bound-back agent, ``ssh-add -l`` listed a key through an agent under the runtime
dir, and a socket named in ``runtime_sockets`` answered while it was absent without it
(levain project_memory/1012-dirmngr/).
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
    monkeypatch.setattr(conf, "_gpg_agent_sockets",
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
            mp.setattr(conf, "_gpg_agent_sockets", lambda: conf_sockets)
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
