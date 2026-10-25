"""3c9-fix4: the replenish keys step (restart when a gw account is created, after the pool refill)."""
from __future__ import annotations

from levain.slot import install_helper as IH, units

KEYS = "levain-keys.service"


def _plan(monkeypatch, *, create_gw: bool):
    from levain.firing import hands
    from levain.slot.grammar import parse_install_argv
    from levain.slot.ids import SINGLETONS
    from tests.test_slot_install_plan import _facts, _set_up, _setup
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
    setup = IH.plan_setup(_setup(), _facts("linux"))
    accounts = dict(setup.allocation.ids)
    gone = {"_levain_s0", "_levain_gw0"} if create_gw else set()
    live = {"egress": range(1, 8), "none": range(2)} if create_gw else {"egress": range(8), "none": range(2)}
    return IH.plan_replenish(
        parse_install_argv(["replenish"]),
        _set_up("linux", used_ids=frozenset(accounts.values()), existing_names=frozenset(accounts)),
        slots=8, verifier_slots=2, live=live,
        accounts={k: v for k, v in accounts.items() if k not in gone and k not in SINGLETONS})


def _whys(plan):
    return [s.why for s in plan.steps]


def test_a_plan_that_creates_a_gw_account_restarts_keys(monkeypatch):
    plan = _plan(monkeypatch, create_gw=True)
    assert any(a.name.startswith("_levain_gw") for a in plan.accounts), plan.accounts
    whys = _whys(plan)
    assert f"stop {KEYS}" in whys and f"enable and start {KEYS}" in whys
    assert whys.index(f"stop {KEYS}") < whys.index(f"enable and start {KEYS}")


def test_a_plan_that_creates_no_gw_account_enables_keys_only(monkeypatch):
    plan = _plan(monkeypatch, create_gw=False)
    whys = _whys(plan)
    assert f"enable and start {KEYS}" in whys and f"stop {KEYS}" not in whys


def test_keys_comes_after_the_pool_refill_and_before_the_intent_removal(monkeypatch):
    whys = _whys(_plan(monkeypatch, create_gw=True))
    start = whys.index(f"enable and start {KEYS}")
    refill = [i for i, w in enumerate(whys) if w.startswith("mark ") or "install-shape" in w]
    assert refill and max(refill) < whys.index(f"stop {KEYS}")
    assert start == len(whys) - 2, whys[start:]


def test_units_builds_the_keys_step_and_the_install_filter_is_gone():
    plain = units.keys_start_steps("linux")
    assert [s.why for s in plain] == [f"enable and start {KEYS}"]
    assert [s.why for s in units.keys_start_steps("linux", restart=True)] == [f"stop {KEYS}", f"enable and start {KEYS}"]
    assert units.keys_start_steps("darwin") == []
    import inspect
    assert ".why ==" not in inspect.getsource(IH._keys_start_steps) + inspect.getsource(IH.plan_replenish)
