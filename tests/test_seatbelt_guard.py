"""levain refuses, before any exec, a Seatbelt profile that would make XNU build a syscall mask.

A malformed syscall mask kernel-panicked a Mac on 2026-10-07 ("mask size exceeds maximum",
syscallmask.c) from a hand-written experiment. levain launches its macOS floor by absolute path, so
a host PATH shim never sees its profiles; the refusal must be levain's own. These tests render and
scan TEXT only and never start a sandbox: the spawn is stopped before anything is executed.
"""
from __future__ import annotations

import pytest

from levain.firing import confinement
from levain.firing.seatbelt_guard import scan


@pytest.mark.parametrize("rule", [
    "(deny syscall-unix (syscall-number SYS_sysctl))",
    "(allow syscall-mach)",
    "(deny syscall-mig)",
    "(deny syscall*)",
    "(deny syscall-group-foo)",
    "(deny machtrap-number 26)",
    "(deny kernel-mig-routine)",
    "(DENY SYSCALL-UNIX)",
])
def test_scan_finds_every_kernel_mask_form(rule) -> None:
    assert scan("(version 1)\n(allow default)\n" + rule + "\n")


@pytest.mark.parametrize("text", [
    "(version 1)\n(allow default)\n(deny sysctl-read (sysctl-name-prefix \"kern.procargs\"))\n",
    "(version 1)\n(allow default)\n(deny file-read* (literal \"/tmp/xsyscall-unixy\"))\n",
    "(version 1)\n(allow default)\n(deny file-read* (subpath \"/Users/x/syscall-unix-notes\"))\n",
])
def test_scan_passes_names_that_only_contain_the_words(text) -> None:
    # The M1 sysctl NAME filter, and paths that merely contain the words, are not mask rules.
    assert scan(text) == []


@pytest.mark.parametrize("form", ["(eval x)", "(string->symbol \"x\")", "(load \"x\")"])
def test_scan_refuses_computed_operation_forms(form) -> None:
    # SBPL is TinyScheme: these can build an operation name the literal scan would not see.
    assert scan("(version 1)\n(allow default)\n" + form + "\n")


@pytest.mark.parametrize("deny_creds", [False, True])
@pytest.mark.parametrize("ssh_mode", ["agent", "raw"])
@pytest.mark.parametrize("deny_localhost", [False, True])
def test_levains_own_rendered_profile_passes_the_guard(tmp_path, ssh_mode, deny_creds, deny_localhost) -> None:
    # A guard that refuses levain's own profile would refuse every shell. The first draft did, on
    # the words "load" and "read" inside the profile's ;; comments.
    entity = tmp_path / "ent"
    (entity / ".levain").mkdir(parents=True)
    policy = confinement.build_policy(
        entity, ssh_mode=ssh_mode, deny_standard_creds=deny_creds,
        deny_localhost_outbound=deny_localhost,
    )
    assert scan(confinement.SeatbeltProvider().render_profile(policy)) == []


def test_a_comment_cannot_hide_a_kernel_mask_token() -> None:
    assert scan("(version 1)\n(allow default)\n;; " + "syscall" + "-unix\n")


def test_levains_own_spawn_refuses_a_profile_carrying_a_mask_rule(tmp_path, monkeypatch) -> None:
    executed: list[object] = []
    monkeypatch.setattr(confinement, "sandbox_exec_available", lambda: True)
    monkeypatch.setattr(
        confinement.SeatbeltProvider, "render_profile",
        lambda self, policy: "(version 1)\n(allow default)\n(deny syscall-unix)\n",
    )
    monkeypatch.setattr(confinement, "_SeatbeltShell", lambda *a, **k: executed.append(a) or None)
    monkeypatch.setattr(confinement.subprocess, "Popen", lambda *a, **k: executed.append(a))

    entity = tmp_path / "ent"
    (entity / ".levain").mkdir(parents=True)
    policy = confinement.build_policy(entity)
    with pytest.raises(confinement.ConfinementError, match="syscall mask"):
        confinement.SeatbeltProvider()._spawn_shell_impl(policy)
    assert executed == []
