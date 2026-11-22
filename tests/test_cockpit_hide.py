"""The cockpit cut (np-021ab5aa): ``Cockpit.hide_panels`` leaves panels off the served home without deleting a line of their code.

Each test names, in its docstring, what it is red with before the kernel has the method (an ``AttributeError`` on ``hide_panels``
for all of them) and what it would still be red with if the method were wrong."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.cockpit import CockpitRegistrationError, ProviderSpec, Read
from levain.cockpit.engine import BOARD_DEFAULT_ID, DECISIONS_ID, RETIRED_PANEL_IDS, Cockpit
from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_activity import _flow_ids
from tests.test_cockpit_k2a import CRED, Rig, _install

_V = lambda ctx: Read(value={})                                                  # noqa: E731


def _listed(ck: Cockpit) -> set[str]:
    """Every panel id the served manifest names anywhere: its panels, and each region's lists and cards."""
    m = ck.manifest(CRED)
    r = m["regions"]
    out = set(m["panels"]) | set(r["hero"]) | set(r["brief"]) | set(r["widgets"]) | set(r["header"])
    for z in r["zones"]:
        out |= set(z["panels"])
        for c in z["cards"]:
            out |= set(c["panels"])
    return out


def _board_provider() -> ProviderSpec:
    return ProviderSpec(id="board", kind="board", title="Work Board", priority="feed", region="operate",
                        read=lambda ctx: Read(value={"columns": [], "as_of": "2026-11-21T00:00:00Z", "errors": [], "unreadable": []}))


def test_a_hidden_panel_is_absent_from_the_served_home_and_its_code_is_intact(tmp_path: Path) -> None:
    """Red with AttributeError (no hide_panels). Wrong method: red if the panel is still in the manifest, still readable by
    id, or if hiding deleted the provider so a cockpit with no hide set no longer registers it."""
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    _flow_ids(ck)
    ck.hide_panels({"episodes", "board", "work_board"})
    assert "episodes" not in _listed(ck) and "episodes" not in ck.panel_ids
    assert ck.panel("episodes", credential_class="none") is None
    assert {"tray", "sky", "state"} <= _listed(ck)                   # what the home keeps is untouched
    # code intact: the provider function and the registration still exist, and an unhidden cockpit still has the panel
    from levain.cockpit import providers
    assert callable(providers._episodes) and callable(Cockpit.register)
    assert "episodes" in build_default_cockpit(src).panel_ids
    fresh = build_default_cockpit(src)
    fresh.hide_panels(())
    assert "episodes" in fresh.panel_ids


def test_hiding_the_board_does_not_bring_the_kernels_back() -> None:
    """Red with AttributeError. Wrong method: red if the kernel's work_board is registered by attach_server, or is restored by
    the board-restore that every unregister path runs, after the provider's board was hidden."""
    ck = Cockpit()
    ck.register(_board_provider())
    ck.hide_panels({"board", BOARD_DEFAULT_ID})
    assert "board" not in ck.panel_ids                                            # the registered board left
    assert BOARD_DEFAULT_ID not in ck.panel_ids                                   # ... and nothing restored the kernel's
    ck.attach_server(None, lambda now: None, board=lambda: {"columns": []})
    assert "board" not in ck.panel_ids and BOARD_DEFAULT_ID not in ck.panel_ids   # the server's own attach does not bring it back
    ck.register(ProviderSpec("other", "metric-grid", "Other", "gauge", _V, region="widgets"))
    ck.unregister("other")                                                        # the public unregister path runs _restore_board_locked
    assert BOARD_DEFAULT_ID not in ck.panel_ids
    # control: the same cockpit with no hide set does get the kernel's board
    plain = Cockpit()
    plain.attach_server(None, lambda now: None, board=lambda: {"columns": []})
    assert BOARD_DEFAULT_ID in plain.panel_ids


def test_a_hidden_discovered_panel_stays_hidden_across_manifests() -> None:
    """Red with AttributeError. Wrong method: red if a hidden discovered id comes back on the second manifest build, is recorded
    in _discovered (a KeyError on the 'no longer returns' branch), or if a panel hidden after it was discovered is not removed."""
    def found(ctx):
        return [ProviderSpec("section:x", "prose", "X", "feed", _V, region="mind"),
                ProviderSpec("agents", "metric-grid", "Agents", "gauge", _V, region="widgets")]

    ck = Cockpit()
    ck.discover(found, retire=True)
    ck.hide_panels({"agents"})
    for _ in range(2):
        m = ck.manifest(CRED)
        assert "section:x" in m["panels"] and "agents" not in m["panels"] and "agents" not in _listed(ck)
    assert "agents" not in ck._discovered and ck.panel("agents", credential_class="none") is None
    assert ck.offer_spec("agents") is None and ck.spec("agents", discover=True) is None

    late = Cockpit()                                                              # discovered first, hidden after: it leaves
    late.discover(found, retire=True)
    assert "agents" in late.manifest(CRED)["panels"]
    late.hide_panels({"agents"})
    assert "agents" not in late.manifest(CRED)["panels"] and "agents" not in late._discovered
    owner_answers = [True]                                                        # the owner returns the hidden id once, then stops
    gone = Cockpit()
    gone.discover(lambda ctx: found(ctx) if owner_answers[0] else [], retire=True)
    gone.hide_panels({"agents"})
    gone.manifest(CRED)
    owner_answers[0] = False
    assert "section:x" not in gone.manifest(CRED)["panels"] and "agents" not in gone._discovered   # no KeyError on "no longer returns"

    plain = Cockpit()                                                             # unhidden (a new cockpit): it returns
    plain.discover(found, retire=True)
    assert "agents" in plain.manifest(CRED)["panels"]


def test_hide_refuses_what_the_band_needs_and_reserved_ids() -> None:
    """Red with AttributeError. Wrong method: red if decisions or a reserved id is accepted, a non-string is accepted, or the
    refusal is not the engine's own registration error."""
    ck = Cockpit()
    with pytest.raises(CockpitRegistrationError):
        ck.hide_panels({DECISIONS_ID})
    for rid in RETIRED_PANEL_IDS:
        with pytest.raises(CockpitRegistrationError):
            ck.hide_panels({rid})
    with pytest.raises(CockpitRegistrationError):
        ck.hide_panels({3})                                                       # type: ignore[arg-type]
    with pytest.raises(CockpitRegistrationError):
        ck.hide_panels("episodes")                                                # a bare string is not a set of ids
    assert ck._hidden == frozenset()                                              # a refused call hides nothing
    ck.hide_panels({"no-such-panel"})                                             # an id that does not exist here is allowed
    assert ck._hidden == frozenset({"no-such-panel"})


def test_hidden_panel_verbs_leave_the_manifest_verbs_map(tmp_path: Path) -> None:
    """Red with AttributeError. Wrong method: red if hiding the edits panel leaves its undo verb in the manifest, or if it
    takes the verbs of panels still shown with it."""
    rig = Rig(tmp_path)
    assert "undo" in rig.ck.manifest(CRED)["verbs"] and "episode_tombstone" in rig.ck.manifest(CRED)["verbs"]
    rig.ck.hide_panels({"edits"})
    verbs = rig.ck.manifest(CRED)["verbs"]
    assert "undo" not in verbs and "episode_tombstone" in verbs


def test_a_generator_of_ids_hides_what_it_names(tmp_path: Path) -> None:
    """Red with a hide_panels that checks a generator and then builds its set from the spent one: nothing would be hidden and no
    error raised."""
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.hide_panels(i for i in ("episodes", "edits"))
    assert "episodes" not in ck.panel_ids and "edits" not in ck.panel_ids


def test_discovery_records_no_owner_for_a_spec_the_registration_skipped() -> None:
    """Red with a discovery loop that records the owner after _register_locked whether or not a spec was registered: the id is in
    _discovered with no spec, and the pass after the owner stops returning it raises KeyError. The simulation: the discoverer's
    answer is filtered while nothing is hidden, and the hide lands (``_hidden`` is set by a wrapper round ``_register_locked``)
    in the window between that filter and the registration, which is what a concurrent hide_panels would do."""
    answers = [[ProviderSpec("x", "metric-grid", "X", "gauge", _V, region="widgets")]]
    ck = Cockpit()
    ck.discover(lambda ctx: answers[0], retire=True)
    real = ck._register_locked

    def hide_then_register(spec: ProviderSpec) -> None:
        ck._hidden = frozenset({"x"})
        real(spec)

    ck._register_locked = hide_then_register                                      # type: ignore[method-assign]
    ck.manifest(CRED)
    assert "x" not in ck._specs and "x" not in ck._discovered                     # no owner without a spec
    answers[0] = []                                                               # the owner stops returning it: the retire pass
    ck.manifest(CRED)
    ck.manifest(CRED)


def test_a_hidden_kernel_board_leaves_no_yield_state() -> None:
    """Red with an _attach_board_locked that registers (a no-op under a hidden id) and then adds the id to _board_yields: that
    stale yield would make a real provider registered under that id later look replaceable."""
    ck = Cockpit()
    ck.hide_panels({BOARD_DEFAULT_ID})
    ck.attach_server(None, lambda now: None, board=lambda: {"columns": []})
    assert BOARD_DEFAULT_ID not in ck.panel_ids
    assert BOARD_DEFAULT_ID not in ck._board_yields


def test_hide_panels_is_called_once() -> None:
    """Red with a hide_panels that replaces the hidden set on every call: a second call would silently un-hide the first call's
    ids (a discovered panel returns while a static one stays gone). A refused call does not use up the one call."""
    ck = Cockpit()
    with pytest.raises(CockpitRegistrationError):
        ck.hide_panels({DECISIONS_ID})                                            # refused: the one call is still unused
    ck.hide_panels({"a"})
    with pytest.raises(CockpitRegistrationError, match="called once"):
        ck.hide_panels({"b"})
    assert ck._hidden == frozenset({"a"})


def test_hide_panels_is_refused_once_a_refresher_has_started() -> None:
    """Red with a hide_panels that unregisters a panel whose refresher thread is running: the thread stays in _refreshers and a
    later re-register would be polled by the old one. The cockpit is the smallest with one refresher panel."""
    ck = Cockpit()
    ck.register(ProviderSpec("r", "metric-grid", "R", "gauge", _V, region="widgets", refresh_every_s=60.0, stale_after_s=60.0))
    ck.start()
    try:
        assert ck.refresher("r") is not None
        with pytest.raises(CockpitRegistrationError, match="before the cockpit starts"):
            ck.hide_panels({"r"})
        assert ck._hidden == frozenset() and "r" in ck.panel_ids
    finally:
        ck.stop()


@pytest.mark.parametrize("bad", [None, 3, 2.5])
def test_hide_panels_refuses_a_non_iterable_with_the_registration_error(bad: object) -> None:
    """Red with a hide_panels that lets the TypeError of tuple(ids) through."""
    ck = Cockpit()
    with pytest.raises(CockpitRegistrationError):
        ck.hide_panels(bad)
    assert ck._hidden == frozenset()
