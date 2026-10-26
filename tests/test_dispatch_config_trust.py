"""3c-9 session B (Q15 r3): ``levain dispatch config init`` pins the route trust through the install helper's
``routes-trust``, and ``config trust`` re-sends it. The real CLI over a real control store and real enrolled keys; the
helper is the REAL ``install_helper.main`` run in-process as root's seams over a scratch L (no sudo)."""
from __future__ import annotations

import functools
import io
import os
import pwd

import pytest

from levain.dispatch import cli as dcli
from levain.slot import install_helper as IH, routemaps, runtime_helper as RH
from tests.test_dispatch_config import ddir, root, run  # noqa: F401  (fixtures)
from tests.test_dispatch_sign import keys, log  # noqa: F401  (module fixtures)

ME = os.getuid()


@pytest.fixture
def L(tmp_path):
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "routes").mkdir()
    (L / "policy" / "operator").write_text(IH.operator_record_text(pwd.getpwuid(ME).pw_name, ME))
    return L


class _Lock:
    def __call__(self, host=None):
        class _Cm:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return None
        return _Cm()


@pytest.fixture
def helper(L, monkeypatch):
    """Root's side: the install helper's own ``main`` with ``routes-trust`` over ``L``; records each request."""
    sent, argvs = [], []

    def call(data: bytes, *, replace: bool = False):
        sent.append(data)
        argvs.append(["routes-trust", *(["--replace"] if replace else [])])
        out, err = io.StringIO(), io.StringIO()
        run_trust = functools.partial(IH.run_routes_trust, store_seams=RH.Seams(host="darwin", store=L,
                                                                                trusted=lambda st: True))
        seams = IH.Seams(euid=lambda: 0, host=lambda: "darwin", lock=_Lock(), stdin=io.BytesIO(data).read,
                         routes_trust=run_trust)
        rc = IH.main(argvs[-1], env={"SUDO_UID": str(ME)}, seams=seams, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()
    monkeypatch.setattr(dcli, "ROUTES_TRUST_HELPER", call)
    return sent


def _init(capsys, ddir, root, keys, *extra):
    return run(capsys, "config", "init", "--trust-root", keys["laptop"].fp, "--dispatch-dir", str(ddir),
               "--data-root", root, *extra)


def test_init_pins_the_route_trust_root_reads_back(capsys, ddir, root, keys, L, helper):
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 0, out
    assert "The route trust is pinned" in out and len(helper) == 1
    t = routemaps.read_trust(L / "routes")
    assert t.fingerprint == keys["laptop"].fp and IH.key_fingerprint(t.public_key) == keys["laptop"].fp
    assert f"(store id {t.store_id})" in out


def test_config_trust_resends_the_same_values_and_root_accepts_them_again(capsys, ddir, root, keys, L, helper):
    _init(capsys, ddir, root, keys)
    code, out, _ = run(capsys, "config", "trust", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0, out
    assert len(helper) == 2 and helper[0] == helper[1]


def test_config_trust_after_the_files_went_writes_them_again(capsys, ddir, root, keys, L, helper):
    _init(capsys, ddir, root, keys)
    for name in ("trust.pub", "trust.json"):
        os.unlink(L / "routes" / name)
    code, out, _ = run(capsys, "config", "trust", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0, out
    assert routemaps.read_trust(L / "routes").fingerprint == keys["laptop"].fp


def test_root_refusing_is_exit_2_naming_the_remedy(capsys, ddir, root, keys, L, helper):
    (L / "routes" / "trust.json").write_text('{"fingerprint":"SHA256:' + "A" * 43 + '","format":"levain-routes-'
                                             'trust/1","store_id":"' + "f" * 32 + '"}')
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 2
    assert "NOT pinned (routes-trust-exists" in out and "levain dispatch config trust" in out


def test_config_trust_replace_rotates_a_pin_naming_another_store(capsys, ddir, root, keys, L, helper):
    """Q16 through the CLI: the plain verb names `--replace` as the remedy, and `config trust --replace` sends root's
    `routes-trust --replace`, which sets the old pin aside and pins this store."""
    old = '{"fingerprint":"SHA256:' + "A" * 43 + '","format":"levain-routes-trust/1","store_id":"' + "f" * 32 + '"}'
    (L / "routes" / "trust.json").write_text(old)
    (L / "sessions").mkdir(mode=0o700)
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 2 and "levain dispatch config trust --replace" in out
    code, out, _ = run(capsys, "config", "trust", "--replace", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0, out
    assert "replacing the pin to store " + "f" * 32 in out
    t = routemaps.read_trust(L / "routes")
    assert t.fingerprint == keys["laptop"].fp and t.store_id != "f" * 32
    aside = [p for p in (L / "routes").iterdir() if p.name.startswith(".replaced-")]
    assert len(aside) == 1 and (aside[0] / "trust.json").read_text() == old


def test_no_install_helper_leaves_init_ok_and_says_so(capsys, ddir, root, keys, monkeypatch):
    monkeypatch.setattr(dcli, "ROUTES_TRUST_HELPER", lambda data: (127, "", "the install helper x is not installed"))
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 0, out
    assert "NOT pinned: slots are not set up" in out
    code, out, _ = run(capsys, "config", "trust", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 2 and "slots are not set up" in out


def test_config_trust_with_no_store_points_at_init(capsys, ddir, root, helper):
    code, out, _ = run(capsys, "config", "trust", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 2 and "config init" in out and helper == []


def test_the_default_helper_is_sudo_routes_trust():
    # the conftest stub stands in for it in every test, so the default is read from the source
    import inspect
    assert "\nROUTES_TRUST_HELPER: Callable[[bytes], tuple[int, str, str]] = _sudo_routes_trust\n" in inspect.getsource(
        dcli)
    assert dcli.ROUTES_TRUST_HELPER is not dcli._sudo_routes_trust and dcli.ROUTES_TRUST_HELPER(b"{}")[0] == 127
    assert dcli.ROUTES_TRUST_REQUEST == IH.ROUTES_TRUST_REQUEST


# --- 3c-9 fix r1 (RULINGS_R1 items 7, 8) ----------------------------------------------------------------------------------

def test_init_says_the_store_was_created_when_the_pin_preflight_refuses(capsys, ddir, root, keys, helper, monkeypatch):
    """Item 8: the trust root's key stops passing the may-sign test between the store's creation and the pin (the
    log changed under it). init says the store exists, that the trust is not pinned, and names ``config trust``."""
    from levain.dispatch import control as dctl
    made = []
    real_init, real_problem = dctl.ControlStore.init, dctl._root_problem
    monkeypatch.setattr(dctl.ControlStore, "init", staticmethod(lambda *a, **k: (real_init(*a, **k), made.append(1))[0]))
    monkeypatch.setattr(dctl, "_root_problem", lambda s, fp: "the key was revoked" if made else real_problem(s, fp))
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 2 and helper == []
    assert "the control store" in out and "(store id " in out
    assert "NOT pinned (preflight: the key was revoked)" in out and "levain dispatch config trust" in out


def test_sudo_that_cannot_run_is_a_named_refusal_not_a_traceback(capsys, ddir, root, keys, tmp_path, monkeypatch):
    """Item 7: an OSError from running sudo is ``sudo-failed``, distinct from slots not being set up."""
    import levain.slot as slot
    from levain.slot import layout
    fake = tmp_path / "install-helper"
    fake.write_text("")
    monkeypatch.setattr(layout, "install_helper_path", lambda host: fake)
    monkeypatch.setattr(slot, "SUDO", str(tmp_path / "no-such-sudo"))
    rc, stdout, stderr = dcli._sudo_routes_trust(b"{}")
    assert rc != 0 and "no-such-sudo" in stderr
    monkeypatch.setattr(dcli, "ROUTES_TRUST_HELPER", dcli._sudo_routes_trust)
    code, out, _ = _init(capsys, ddir, root, keys)
    assert code == 2
    assert "NOT pinned (sudo-failed" in out and "slots are not set up" not in out
