"""levain.firing.crystallization — the unattended-consolidate bound (K4a [6]).

THE INVARIANT UNDER TEST: an unattended consolidate MAY METABOLIZE (compose the working neocortex)
but MAY NEVER CRYSTALLIZE (promote a pattern into the always-loaded bedrock tier).

Pure-stdlib leaf, so these run with no anneal store and no openhands extra. The end-to-end proof
that a real wrap honours the bound lives in `test_wrap_unattended.py`; this file pins the mechanism.
"""
from __future__ import annotations

import pytest
from levain.firing.crystallization import (
    CRYSTAL_READS,
    CrystallizationRefused,
    NoCrystallizeStore,
    refuse_crystallization,
)


class _FakeCrystalStore:
    """Stands in for anneal's CrystalStore with both halves of its surface."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    # -- the reads the consolidate legitimately needs --
    def active(self):
        return [{"name": "a_pattern", "level": 3}]

    def surface_rewarm_candidates(self, *, today=None):
        return [{"name": "a_hot_pattern"}]

    # -- the writes it must never reach --
    def crystallize(self, *args, **kwargs):
        self.writes.append("crystallize")
        return {"name": "promoted"}

    def touch(self, name, *, today=None):
        self.writes.append("touch")

    def update(self, name, **kwargs):
        self.writes.append("update")

    def retire(self, name, **kwargs):
        self.writes.append("retire")


# ---------- the reads pass through ----------

def test_allowed_reads_pass_through_untouched():
    store = _FakeCrystalStore()
    proxy = refuse_crystallization(store)
    assert proxy.active() == [{"name": "a_pattern", "level": 3}]
    assert proxy.surface_rewarm_candidates(today=None) == [{"name": "a_hot_pattern"}]


def test_the_allow_list_is_exactly_the_consolidate_path_reads():
    # DERIVED from anneal's call sites, not from CrystalStore's public surface: `continuity.py`
    # reaches for exactly these two on a crystal store across prepare_wrap →
    # validated_save_continuity. Pinned so widening it is a deliberate, reviewable act.
    assert CRYSTAL_READS == {"active", "surface_rewarm_candidates"}


# ---------- every write refuses ----------

@pytest.mark.parametrize("method", ["crystallize", "touch", "update", "retire"])
def test_every_write_method_is_refused_and_never_reaches_the_store(method):
    store = _FakeCrystalStore()
    proxy = refuse_crystallization(store)
    with pytest.raises(CrystallizationRefused) as exc:
        getattr(proxy, method)
    assert method in str(exc.value)
    # THE LOAD-BEARING HALF: refused BEFORE the call, so the underlying store never mutated.
    assert store.writes == []


def test_an_unknown_future_method_is_refused_by_default():
    """DENY-BY-DEFAULT IS THE WHOLE POLARITY ARGUMENT, so it gets its own test.

    A deny-list of the four writes known today would let anneal introduce a fifth and have it pass
    through silently. The allow-list means a method nobody has heard of arrives REFUSED — the same
    choice K3's gate made for an unrecognised tool, and the reason this is a proxy rather than a
    subclass with four overrides."""
    proxy = refuse_crystallization(_FakeCrystalStore())
    with pytest.raises(CrystallizationRefused):
        proxy.some_method_anneal_adds_in_2027


def test_attribute_assignment_is_refused_too():
    # Mutating state through the proxy is a write by another route.
    proxy = refuse_crystallization(_FakeCrystalStore())
    with pytest.raises(CrystallizationRefused):
        proxy.level = 99


# ---------- the swallowing property ----------

def test_refusal_is_not_catchable_by_except_exception():
    """THE PROPERTY THAT MAKES THE BOUND REAL, tested behaviourally rather than by `issubclass`.

    K4a ⑥ learned this on the device: the SDK's broad `except Exception` handlers swallowed a
    timeout, called it an LLM error and retried — while the unit suite stayed green over it. The
    consolidate path is deliberately full of fail-soft handlers (a crystal fault must never break a
    wrap), so a refusal that `except Exception` can eat would be a boundary that reports itself
    armed and does nothing. An `issubclass` assertion alone would not prove the handler behaviour;
    this does."""
    proxy = refuse_crystallization(_FakeCrystalStore())
    swallowed = False
    try:
        try:
            proxy.crystallize
        except Exception:  # noqa: BLE001 — the point of the test
            swallowed = True
    except CrystallizationRefused:
        pass
    assert swallowed is False, "an `except Exception` swallowed the crystallization refusal"


def test_refusal_derives_from_baseexception_not_exception():
    assert issubclass(CrystallizationRefused, BaseException)
    assert not issubclass(CrystallizationRefused, Exception)


# ---------- shape ----------

def test_refuse_crystallization_returns_the_proxy_type():
    assert isinstance(refuse_crystallization(_FakeCrystalStore()), NoCrystallizeStore)


def test_repr_names_the_wrapped_store_for_debuggability():
    assert "_FakeCrystalStore" in repr(refuse_crystallization(_FakeCrystalStore()))
