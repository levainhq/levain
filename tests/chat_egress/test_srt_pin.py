"""CH-D2 S8 (second slice): srt on the machine (`levain.chat_egress.srt_pin`; P-5, R15-10-R2 (4)).

The install-time vendor check reads the glue's own srt imports and fails visibly against an srt build whose import
names moved; `setup` runs `npm ci --ignore-scripts` of the shipped lock into a temp prefix (offline: from the npm
cache, no network at test time) and `check` refuses a hash mismatch (a changed file, a changed npm record, another
lock) and a missing install.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
from pathlib import Path

import pytest

from levain.chat_egress import srt_pin

from .fixtures import env as envmod

GLUE_TAKES = {
    "decider-client.js": ("createDecider",),
    "emitted-connection.js": ("keepFlushedHeaderBatches", "writeHeadsAsLatin1"),
    "http-proxy.js": ("createHttpProxyServer", "markHandedInConnection"),
    "mitm-ca.js": ("createMitmCA", "disposeMitmCASync"),
    "parent-proxy.js": ("canonicalizeHost",),
    "resolved-address-guard.js": ("createResolvedAddressGuard",),
}


def test_the_glue_imports_are_read_from_the_glue_itself() -> None:
    assert srt_pin.glue_imports() == GLUE_TAKES


def test_the_default_prefix_is_under_the_levain_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path))
    assert srt_pin.default_prefix() == tmp_path.resolve() / "chat-node"


@pytest.mark.chat_egress_srt
def test_the_vendor_check_passes_on_the_pinned_srt() -> None:
    assert srt_pin.vendor_check(envmod.srt_package_dir() / "dist" / "sandbox") == []


@pytest.mark.chat_egress_srt
def test_the_vendor_check_fails_visibly_when_an_import_name_moved(tmp_path) -> None:
    dist = tmp_path / "dist"
    shutil.copytree(envmod.srt_package_dir() / "dist" / "sandbox", dist)
    src = (dist / "http-proxy.js").read_text()
    assert "export function markHandedInConnection" in src
    (dist / "http-proxy.js").write_text(src.replace("export function markHandedInConnection",
                                                    "export function markHandedInConnection2"))
    (dist / "parent-proxy.js").unlink()
    assert srt_pin.vendor_check(dist) == ["http-proxy.js: markHandedInConnection", "parent-proxy.js: (module missing)"]


def test_check_refuses_when_nothing_is_set_up(tmp_path) -> None:
    with pytest.raises(srt_pin.SrtProblem, match="not set up"):
        srt_pin.check(tmp_path / "none")


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    if shutil.which("npm") is None:
        pytest.skip("npm is not on PATH")
    prefix = tmp_path_factory.mktemp("chatnode") / "chat-node"
    try:
        # the tests' HOME is a scratch dir (tests/conftest.py), so npm is pointed at the operator's cache, offline
        st = srt_pin.setup(prefix, offline=True, cache=Path(pwd.getpwuid(os.getuid()).pw_dir) / ".npm")
    except srt_pin.SrtProblem as exc:
        if "ENOTCACHED" in str(exc) or "offline" in str(exc):
            pytest.skip(f"srt 0.0.81 is not in the npm cache (no network at test time): {exc}")
        raise
    return prefix, st


@pytest.mark.chat_egress_srt
def test_setup_installs_the_pinned_srt_and_check_accepts_it(installed) -> None:
    prefix, st = installed
    print(f"RUN S8B setup: {st}")
    assert st.version == "0.0.81" and st.dist == prefix / "node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox"
    assert srt_pin.check(prefix) == st
    assert json.loads((prefix / "package-lock.json").read_text()) == \
        json.loads((srt_pin.NODE_DIR / "package-lock.json").read_text())


def _clone(installed, tmp_path) -> Path:
    prefix = tmp_path / "chat-node"
    shutil.copytree(installed[0], prefix, symlinks=True)
    return prefix


@pytest.mark.chat_egress_srt
def test_check_refuses_a_changed_srt_file(installed, tmp_path) -> None:
    prefix = _clone(installed, tmp_path)
    f = srt_pin.srt_dist(prefix) / "http-proxy.js"
    f.write_text(f.read_text() + "\n// tampered\n")
    with pytest.raises(srt_pin.SrtProblem, match="hash mismatch: node_modules/@anthropic-ai/sandbox-runtime's files"):
        srt_pin.check(prefix)


@pytest.mark.chat_egress_srt
def test_check_refuses_an_npm_record_with_another_integrity(installed, tmp_path) -> None:
    prefix = _clone(installed, tmp_path)
    rec = prefix / "node_modules" / ".package-lock.json"
    d = json.loads(rec.read_text())
    d["packages"]["node_modules/zod"]["integrity"] = "sha512-" + "A" * 86 + "=="
    rec.write_text(json.dumps(d))
    with pytest.raises(srt_pin.SrtProblem, match="hash mismatch: node_modules/zod"):
        srt_pin.check(prefix)


@pytest.mark.chat_egress_srt
def test_check_refuses_an_install_from_another_lock(installed, tmp_path) -> None:
    prefix = _clone(installed, tmp_path)
    stamp = prefix / "levain-srt.json"
    d = json.loads(stamp.read_text())
    d["lock_sha256"] = "0" * 64
    stamp.write_text(json.dumps(d))
    with pytest.raises(srt_pin.SrtProblem, match="another lock"):
        srt_pin.check(prefix)


def test_doctor_says_chat_network_is_not_set_up_in_words(tmp_path, monkeypatch) -> None:
    from levain import doctor
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path))
    [row] = doctor._check_chat_network()
    assert row.neutral and "not set up" in row.detail and "levain chat setup" in row.detail


@pytest.mark.chat_egress_srt
def test_doctor_reports_node_and_srt_and_refuses_a_hash_mismatch(installed, tmp_path, monkeypatch) -> None:
    from levain import doctor
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path))
    prefix = _clone(installed, tmp_path)
    monkeypatch.setenv("PATH", str(Path(envmod.node_bin()).parent) + ":/usr/bin:/bin")
    [ok] = doctor._check_chat_network()
    print(f"RUN S8B doctor: {ok}")
    assert ok.ok and not ok.neutral and "node v26." in ok.detail and "srt 0.0.81" in ok.detail
    f = srt_pin.srt_dist(prefix) / "mitm-ca.js"
    f.write_text(f.read_text() + "\n")
    [bad] = doctor._check_chat_network()
    assert not bad.ok and "hash mismatch" in bad.detail


# --- S8b r1 item 2: every package the lock names, and nothing beside them -------------------------------------------

@pytest.mark.chat_egress_srt
def test_check_refuses_a_changed_transitive_dependency_file(tmp_path) -> None:
    prefix = envmod.srt_prefix_copy(tmp_path / "chat-node")
    f = prefix / "node_modules/node-forge/lib/forge.js"
    f.write_text(f.read_text() + "\n// tampered after setup\n")
    with pytest.raises(srt_pin.SrtProblem, match="hash mismatch: node_modules/node-forge") as got:
        srt_pin.check(prefix)
    print(f"RUN S8D item 2: node-forge forge.js changed -> {got.value}")


@pytest.mark.chat_egress_srt
def test_check_refuses_a_package_planted_where_node_resolves_before_the_locked_one(tmp_path) -> None:
    prefix = envmod.srt_prefix_copy(tmp_path / "chat-node")
    # srt's `require('node-forge')` looks in node_modules/@anthropic-ai/node_modules before node_modules/
    shutil.copytree(prefix / "node_modules/node-forge", prefix / "node_modules/@anthropic-ai/node_modules/node-forge")
    with pytest.raises(srt_pin.SrtProblem, match="outside every package the lock names"):
        srt_pin.check(prefix)


@pytest.mark.chat_egress_srt
def test_check_refuses_a_locked_file_swapped_for_a_symlink(tmp_path) -> None:
    prefix = envmod.srt_prefix_copy(tmp_path / "chat-node")
    f = prefix / "node_modules/node-forge/lib/forge.js"
    evil = tmp_path / "forge.js"
    evil.write_text(f.read_text() + "\n// elsewhere\n")
    f.unlink()
    f.symlink_to(evil)
    with pytest.raises(srt_pin.SrtProblem, match="hash mismatch: node_modules/node-forge"):
        srt_pin.check(prefix)


# --- S8b r1 item 5: every decode, shape and filesystem failure is an SrtProblem -------------------------------------

def _rewrite_json(path: Path, edit) -> None:
    d = json.loads(path.read_text())
    path.write_text(json.dumps(edit(d)))


SHAPES = {
    "stamp-a-list": lambda p: (p / "levain-srt.json").write_text("[]"),
    "stamp-trees-a-list": lambda p: _rewrite_json(p / "levain-srt.json", lambda d: {**d, "trees": []}),
    "npm-packages-a-list": lambda p: _rewrite_json(p / "node_modules/.package-lock.json",
                                                   lambda d: {**d, "packages": []}),
    "npm-entry-a-string": lambda p: _rewrite_json(p / "node_modules/.package-lock.json",
                                                  lambda d: {**d, "packages": {**d["packages"],
                                                                               "node_modules/zod": "x"}}),
    "npm-record-not-utf8": lambda p: (p / "node_modules/.package-lock.json").write_bytes(b"\xff\xfe{"),
    "srt-package-json-a-list": lambda p: (p / "node_modules/@anthropic-ai/sandbox-runtime/package.json")
    .write_text("[]"),
    "unreadable-locked-file": lambda p: (p / "node_modules/node-forge/lib/forge.js").chmod(0o000),
    "unreadable-dir": lambda p: (p / "node_modules/zod").chmod(0o000),
}


@pytest.mark.chat_egress_srt
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_check_turns_every_malformed_install_into_an_srt_problem(shape, tmp_path) -> None:
    prefix = envmod.srt_prefix_copy(tmp_path / "chat-node")
    SHAPES[shape](prefix)
    try:
        with pytest.raises(srt_pin.SrtProblem) as got:
            srt_pin.check(prefix)
    finally:
        for p in (prefix / "node_modules/node-forge/lib/forge.js", prefix / "node_modules/zod"):
            p.chmod(0o700)                                   # let pytest's tmp cleanup in
    print(f"RUN S8D item 5 {shape}: {got.value}")


def test_the_vendor_check_names_a_module_that_is_not_text(tmp_path) -> None:
    (tmp_path / "http-proxy.js").write_bytes(b"\xff\xfe\x00")
    assert srt_pin.vendor_check(tmp_path, {"http-proxy.js": ("createHttpProxyServer",)}) == \
        ["http-proxy.js: (module unreadable)"]


def test_setup_refuses_a_prefix_it_cannot_create(tmp_path) -> None:
    (tmp_path / "chat-node").write_text("a file where the prefix goes")
    with pytest.raises(srt_pin.SrtProblem):
        srt_pin.setup(tmp_path / "chat-node", npm="/usr/bin/false")


def test_doctor_reports_a_malformed_stamp_never_crashes(tmp_path, monkeypatch) -> None:
    from levain import doctor
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path))
    (tmp_path / "chat-node").mkdir()
    (tmp_path / "chat-node" / "levain-srt.json").write_text("[]")
    monkeypatch.setattr("levain.chat_egress.launch.check_node", lambda node: "v26.8.1")
    [row] = doctor._check_chat_network()
    print(f"RUN S8D item 5 doctor stamp []: {row}")
    assert not row.ok and "levain chat setup" in row.detail


def test_doctor_reports_an_unreadable_prefix_never_crashes(tmp_path, monkeypatch) -> None:
    from levain import doctor
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path))
    (tmp_path / "chat-node").mkdir()
    (tmp_path / "chat-node").chmod(0o000)
    try:
        [row] = doctor._check_chat_network()
    finally:
        (tmp_path / "chat-node").chmod(0o700)
    print(f"RUN S8D item 5 doctor unreadable prefix: {row}")
    assert not row.ok
