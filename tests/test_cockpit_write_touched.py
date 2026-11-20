"""levain#157 W: a cockpit verb answers with what it touched (``touched``, and ``row`` where the write held the record), and
only those panels' cached reads go cold. The HTTP tests drive the same snooze ``test_cockpit_k2a`` fires over a real server."""
from __future__ import annotations

import dataclasses
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import pytest

from levain.cockpit.engine import Cockpit
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.verbs import VerbRegistry
from tests.test_cockpit_k2a import TOKEN, _get, _install, _post, _serve


def _snooze_body(base: str, ids: dict[str, str], key: str = "seed", **extra) -> dict:
    rows = _get(f"{base}/cockpit/panel/tray.json", TOKEN)["rows"]
    row = next(r for r in rows if r["id"] == f"intention:{ids[key]}")
    return {"verb": "intention_surface_at", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
            "params": {"surface_at": "2030-01-01"}, **extra}


def _scopes(ck: Cockpit) -> set:
    return {k[2][1] for k in ck._rc if isinstance(k[2], tuple) and k[2][0] == "panel"}


def test_a_snooze_answers_with_touched_and_the_row_it_left(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        st, body = _post(f"{base}/cockpit/verb", _snooze_body(base, ids), TOKEN)
        assert st == 200 and body["ok"] is True, body
        assert body["touched"] and "tray" in body["touched"] and "edits" in body["touched"]
        assert len(body["touched"]) == len(set(body["touched"]))
        row = body["row"]
        assert row["id"] == f"intention:{ids['seed']}" and row["next"] == "2030-01-01"
        # the version the answer carries is the one the panel now renders
        fresh = next(r for r in _get(f"{base}/cockpit/panel/tray.json", TOKEN)["rows"] if r["id"] == row["id"])
        assert row["row_version"] == fresh["version"]


def test_a_dry_run_has_no_touched_and_a_second_snooze_on_the_new_version_lands(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        st, dry = _post(f"{base}/cockpit/verb", _snooze_body(base, ids, dry_run=True), TOKEN)
        assert st == 200 and dry["dry_run"] is True and "touched" not in dry and "row" not in dry
        st, first = _post(f"{base}/cockpit/verb", _snooze_body(base, ids), TOKEN)
        assert st == 200
        body = _snooze_body(base, ids)
        body["row_version"] = first["row"]["row_version"]          # the CAS key the answer handed back
        body["params"] = {"surface_at": "2031-01-01"}
        st, again = _post(f"{base}/cockpit/verb", body, TOKEN)
        assert st == 200 and again["row"]["next"] == "2031-01-01", again


def test_the_edits_ledger_row_carries_received_at(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        st, _ = _post(f"{base}/cockpit/verb", _snooze_body(base, ids), TOKEN)
        assert st == 200
    rows = [json.loads(line) for line in (root / ".levain" / "edits.jsonl").read_text().splitlines() if line]
    mine = [r for r in rows if r.get("kind") == "intention_surface_at"]
    assert mine and mine[-1]["received_at"]
    # parsed, not compared as text: isoformat leaves the fraction out at microsecond 0, which reorders the strings
    assert datetime.fromisoformat(mine[-1]["ts"]) >= datetime.fromisoformat(mine[-1]["received_at"])


def test_a_verb_post_prints_its_time_and_a_slow_one_warns(tmp_path: Path, capsys, caplog, monkeypatch) -> None:
    import levain.web_server as ws

    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        _post(f"{base}/cockpit/verb", _snooze_body(base, ids), TOKEN)
        # the line is written after the response: wait for the handler's own thread to get there
        for _ in range(100):
            if "cockpit write" in capsys.readouterr().err + "":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("no timing line")
        monkeypatch.setattr(ws, "_SLOW_READ_MS", -1.0)
        with caplog.at_level("WARNING"):
            _post(f"{base}/cockpit/verb", _snooze_body(base, ids, "loop"), TOKEN)
            for _ in range(100):
                if any("slow cockpit write" in r.getMessage() for r in caplog.records):
                    break
                threading.Event().wait(0.05)
        assert any("slow cockpit write: verb=intention_surface_at" in r.getMessage() for r in caplog.records)


def test_the_timing_line_shape(tmp_path: Path, capfd) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        _post(f"{base}/cockpit/verb", _snooze_body(base, ids), TOKEN)
        out = ""
        for _ in range(100):
            out += capfd.readouterr().err
            if re.search(r"^cockpit write intention_surface_at \d+ ms$", out, re.M):
                break
            threading.Event().wait(0.05)
        assert re.search(r"^cockpit write intention_surface_at \d+ ms$", out, re.M), out


def test_a_snooze_over_http_leaves_an_untouched_panels_cached_read_in_place(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        ck = httpd.get_cockpit()
        _get(f"{base}/cockpit/manifest.json", TOKEN)
        _get(f"{base}/cockpit/panel/health.json", TOKEN)
        body = _snooze_body(base, ids)                 # reads the tray panel: its entry is cached too
        assert {"tray", "health"} <= _scopes(ck)
        health = {k: e for k, e in ck._rc.items() if isinstance(k[2], tuple) and k[2][:2] == ("panel", "health")}
        st, out = _post(f"{base}/cockpit/verb", body, TOKEN)
        assert st == 200 and "health" not in out["touched"]
        assert "tray" not in _scopes(ck)               # the touched panel's entries went cold
        assert not any(k[2][0] == "manifest" for k in ck._rc if isinstance(k[2], tuple))      # and the manifest's
        assert all(ck._rc.get(k) is e for k, e in health.items())                             # health's stayed


def test_a_dry_run_post_drops_no_cached_read(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        ck = httpd.get_cockpit()
        _get(f"{base}/cockpit/manifest.json", TOKEN)
        body = _snooze_body(base, ids, dry_run=True)
        warm, gen = dict(ck._rc), ck._rc_gen
        st, out = _post(f"{base}/cockpit/verb", body, TOKEN)
        assert st == 200 and out["dry_run"] is True
        threading.Event().wait(0.3)
        assert ck._rc_gen == gen and ck._rc == warm


def test_a_refused_verb_still_drops_every_read(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        ck = httpd.get_cockpit()
        body = _snooze_body(base, ids)
        body["row_version"] = "0" * 16                 # stale: refused 409, the unknown-effect path keeps today's drop
        gen = ck._rc_gen
        st, out = _post(f"{base}/cockpit/verb", body, TOKEN)
        assert st == 409 and ck._rc_gen > gen and not ck._rc


# --- the keyed drop in the engine ----------------------------------------------------------------------------


@pytest.fixture()
def ck(tmp_path: Path) -> Cockpit:
    _root, src, _ids = _install(tmp_path)
    c = build_default_cockpit(src)
    c.read_cache_ttl_s = 30.0
    return c


def _count_panel_computes(ck: Cockpit, monkeypatch) -> dict[str, int]:
    n: dict[str, int] = {}
    real = ck._panel_compute

    def counting(panel_id, **kw):
        n[panel_id] = n.get(panel_id, 0) + 1
        return real(panel_id, **kw)

    monkeypatch.setattr(ck, "_panel_compute", counting)
    return n


def test_a_keyed_invalidate_makes_the_touched_panel_re_read_and_keeps_the_others(ck, monkeypatch) -> None:
    n = _count_panel_computes(ck, monkeypatch)
    ck.panel("tray", credential_class="token")                  # warm-up: a panel's first provider read moves the
    ck.panel("health", credential_class="token")                # commit count that is part of its cache key (#138)
    n.clear()
    a = ck.panel("tray", credential_class="token")
    h = ck.panel("health", credential_class="token")
    assert n == {"tray": 1, "health": 1}
    assert ck.panel("tray", credential_class="token") is a and ck.panel("health", credential_class="token") is h
    cred = {"class": "token", "name": "browser", "device_class": None}
    m = ck.manifest(cred)
    gen = ck._rc_gen
    ck.invalidate_reads(["tray"])
    assert ck._rc_gen == gen                                   # not the global generation
    assert ck.panel("health", credential_class="token") is h and n["health"] == 1
    assert ck.panel("tray", credential_class="token") is not a and n["tray"] == 2
    assert ck.manifest(cred) is not m                          # the manifest is always dropped


def test_an_in_flight_read_of_a_touched_panel_is_not_shared_after_the_write(ck, monkeypatch) -> None:
    gate, started = threading.Event(), threading.Event()
    real, calls = ck._panel_compute, []

    def slow(panel_id, **kw):
        calls.append(panel_id)
        if panel_id == "tray" and calls.count("tray") == 1:
            started.set()
            gate.wait(10)
        return real(panel_id, **kw)

    monkeypatch.setattr(ck, "_panel_compute", slow)
    res: list = []
    t = threading.Thread(target=lambda: res.append(ck.panel("tray", credential_class="token")))
    t.start()
    assert started.wait(10)
    ck.invalidate_reads(["tray"])                              # the write lands while the read is out
    after = ck.panel("tray", credential_class="token")         # computes for itself, does not join the older flight
    assert calls.count("tray") == 2
    gate.set()
    t.join(10)
    assert res and res[0] is not after
    stored = [e.value for e in list(ck._rc.values()) if e.done.is_set()]
    assert any(v is after for v in stored)                      # the post-write read is stored (the accessor sees values)
    assert not any(v is res[0] for v in stored)                 # the pre-write owner's result is stored nowhere
    settled = ck.panel("tray", credential_class="token")        # the post-write read moved the commit count in the key (#138)
    assert settled is not res[0]                                # the pre-write owner's result was never stored
    assert ck.panel("tray", credential_class="token") is settled


def test_an_in_flight_read_of_an_untouched_panel_is_still_shared(ck, monkeypatch) -> None:
    gate, started = threading.Event(), threading.Event()
    real, calls = ck._panel_compute, []

    def slow(panel_id, **kw):
        calls.append(panel_id)
        started.set()
        gate.wait(10)
        return real(panel_id, **kw)

    monkeypatch.setattr(ck, "_panel_compute", slow)
    res: list = []
    t = threading.Thread(target=lambda: res.append(ck.panel("health", credential_class="token")))
    t.start()
    assert started.wait(10)
    ck.invalidate_reads(["tray"])
    t2 = threading.Thread(target=lambda: res.append(ck.panel("health", credential_class="token")))
    t2.start()
    threading.Event().wait(0.3)
    gate.set()
    t.join(10)
    t2.join(10)
    assert calls == ["health"] and res[0] is res[1]


def test_the_no_argument_invalidate_is_the_global_drop(ck, monkeypatch) -> None:
    n = _count_panel_computes(ck, monkeypatch)
    ck.panel("tray", credential_class="token")
    ck.panel("health", credential_class="token")
    gen = ck._rc_gen
    ck.invalidate_reads()
    assert ck._rc_gen == gen + 1 and not ck._rc
    ck.panel("tray", credential_class="token")
    ck.panel("health", credential_class="token")
    assert n == {"tray": 2, "health": 2}


def test_a_refresher_panel_in_the_set_is_refreshed_once_on_a_background_thread(ck, monkeypatch) -> None:
    seen: list[threading.Thread] = []
    done = threading.Event()

    def fake_refresh(panel_id: str) -> None:
        seen.append(threading.current_thread())
        done.set()

    monkeypatch.setattr(ck, "refresh", fake_refresh)
    monkeypatch.setitem(ck._refreshers, "tray", object())
    ck.invalidate_reads(["tray", "health"])
    assert done.wait(10)
    threading.Event().wait(0.1)
    assert len(seen) == 1 and seen[0] is not threading.current_thread()
    ck.invalidate_reads(["tray"], refresh=False)                # the server's second call for the same write
    threading.Event().wait(0.2)
    assert len(seen) == 1


# --- the touched rule ---------------------------------------------------------------------------------------


def test_touched_is_the_panels_offering_the_verb_plus_the_verbs_affects(ck) -> None:
    reg = VerbRegistry()
    snooze = reg.get("intention_surface_at")
    got = reg.touched(ck, snooze, "tray")
    assert "tray" in got and "begin" in got and "edits" in got and "health" not in got
    assert got == [p for p in ck.panel_ids if p in got]        # panel order, no repeats
    # a downstream's panel is added by the registry hook; one that is not registered is dropped
    reg.add_affects("intention_surface_at", ["no-such-panel", "health"])
    got = reg.touched(ck, snooze, "tray")
    assert "health" in got and "no-such-panel" not in got
    # a band verb names the List and the derived panels whichever panel it is fired on
    band = reg.get("intention_snooze")
    assert {"tray", "begin"} <= set(reg.touched(ck, band, "decisions"))


def test_a_verb_that_names_nothing_touches_every_panel(ck) -> None:
    reg = VerbRegistry()
    ghost = dataclasses.replace(reg.get("intention_surface_at"), name="offered_nowhere", affects=())
    assert reg.touched(ck, ghost, "no-such-panel") == ck.panel_ids
    with pytest.raises(ValueError):
        reg.add_affects("no_such_verb", ["tray"])


# --- the under-report class: a write touches every panel that reads the store it changed ---------------------------

# the stores a kernel verb changes, and the panels (by id) that read each store, from the providers' own readers. The
# table is the test's own: a new kernel verb has to be placed in it, and a panel that reads a store has to be named here.
_READERS = {
    "intentions": {"tray", "begin", "memory_glance", "edits"},
    "episodes": {"episodes", "health", "activity", "memory_glance", "begin", "graph"},
    "questions": {"decisions", "tray", "activity", "topology"},
    "review": {"review-queue", "coord-morning"},
    "drops": {"coord-status", "coord-morning"},
    "agents": {"agents"},
    "ledger": {"edits"},
}
_WRITES = {
    "intention_touch": ("intentions", "ledger"), "intention_surface_at": ("intentions", "ledger"),
    "intention_update": ("intentions", "ledger"), "intention_retarget": ("intentions", "ledger"),
    "intention_then": ("intentions", "ledger"), "intention_set_disposition": ("intentions", "ledger"),
    "intention_seed": ("intentions", "ledger"),
    "intention_keep": ("intentions", "ledger", "episodes"),
    # a resolve wakes what waited on it into the band (``woke``)
    "intention_close": ("intentions", "ledger", "questions"), "intention_done_once": ("intentions", "ledger", "questions"),
    "intention_stop_series": ("intentions", "ledger", "questions"), "intention_promote": ("intentions", "ledger", "questions"),
    "intention_snooze": ("intentions", "ledger", "questions"), "intention_edit": ("intentions", "ledger", "questions"),
    "intention_done": ("intentions", "ledger", "questions"), "intention_clear": ("intentions", "ledger", "questions"),
    "band_agenda": ("intentions", "ledger", "questions"),
    "list_intake": ("intentions", "ledger", "drops"),
    "episode_tombstone": ("episodes", "ledger"),
    "operator_state": ("episodes", "ledger"),               # the line's felt episode is recorded with it
    "question_answer": ("questions",), "band_answer": ("questions",), "note_dismiss": ("questions",),
    "review_clear": ("review",), "review_reopen": ("review",), "review_clear_signed": ("review",),
    "agent_pause": ("agents",), "agent_resume": ("agents",), "agent_renew": ("agents",), "agent_revoke": ("agents",),
    "agent_retire": ("agents",),
}
_UNNAMED = {"undo", "section_edit", "config", "entity_name"}      # what they change cannot be named: every panel


def test_every_kernel_verb_is_placed_in_the_write_table() -> None:
    from levain.cockpit.verbs import KERNEL_VERBS

    assert {v.name for v in KERNEL_VERBS} == set(_WRITES) | _UNNAMED


@pytest.mark.parametrize("name", sorted(_WRITES))
def test_a_verb_names_every_panel_that_reads_what_it_writes(name: str) -> None:
    from levain.cockpit.verbs import KERNEL_VERBS

    verb = next(v for v in KERNEL_VERBS if v.name == name)
    need = set().union(*(_READERS[s] for s in _WRITES[name]))
    assert verb.affects, f"{name} names no panel"
    assert need <= set(verb.affects), f"{name} leaves out {sorted(need - set(verb.affects))}"


@pytest.mark.parametrize("name", sorted(_UNNAMED))
def test_a_verb_whose_effect_cannot_be_named_touches_every_panel(name: str, ck) -> None:
    reg = VerbRegistry()
    assert reg.touched(ck, reg.get(name), "edits") == ck.panel_ids


def test_a_legacy_kind_verb_always_names_the_ledger() -> None:
    from levain.cockpit.verbs import KERNEL_VERBS

    for v in KERNEL_VERBS:
        if v.legacy_kind:
            assert "edits" in v.affects or "*" in v.affects, v.name


def test_the_ids_the_table_names_are_the_panels_ids() -> None:
    from levain.cockpit.agents_panel import AGENTS_ID
    from levain.cockpit.desk_panels import MORNING_ID, REVIEW_ID
    from levain.cockpit.desk_status import COORD_STATUS_ID
    from levain.cockpit.engine import DECISIONS_ID

    assert (AGENTS_ID, MORNING_ID, REVIEW_ID, COORD_STATUS_ID, DECISIONS_ID) == (
        "agents", "coord-morning", "review-queue", "coord-status", "decisions")


def test_a_keep_over_a_real_install_touches_what_it_changed(tmp_path: Path) -> None:
    from tests.test_cockpit_k2a import Rig

    rig = Rig(tmp_path)
    out = rig.post("intention_keep", "tray", "note", {}, confirm=True)
    assert {"episodes", "health", "activity", "memory_glance", "begin", "tray", "edits"} <= set(out["touched"])


# --- a handler's own ``touched`` adds to the registry's answer, for registered panels ------------------------------


def test_a_handlers_own_touched_adds_to_the_registrys_answer_for_registered_panels(ck) -> None:
    from levain.cockpit.verbs import _landed

    reg = VerbRegistry()
    verb = reg.get("intention_surface_at")
    base = reg.touched(ck, verb, "tray")
    assert "health" not in base
    mixed = _landed(reg, ck, verb, "tray", {"ok": True, "touched": ["health", "no-such-panel", 7, "health"]})
    assert set(mixed["touched"]) == set(base) | {"health"}         # added to, never narrowed to, the registry's set
    assert mixed["touched"] == [p for p in ck.panel_ids if p in mixed["touched"]]
    assert len(mixed["touched"]) == len(set(mixed["touched"]))
    for bad in (["no-such-panel"], [], "tray", None, 3):
        got = _landed(reg, ck, verb, "tray", {"ok": True, "touched": bad})["touched"]
        assert got == base, bad
    # a supplied set that names a panel the registry already reports changes nothing
    assert _landed(reg, ck, verb, "tray", {"ok": True, "touched": ["tray"]})["touched"] == base


def _downstream_rig(tmp_path: Path):
    from levain.cockpit import ProviderSpec, Read
    from levain.cockpit.verbs import VerbSpec
    from tests.test_cockpit_k2a import Rig

    spec = VerbSpec("note_it", "note", "records a note", "panel", fields=("text",), floor="C1",
                    tier_fn=lambda *a: "C1", idempotent=True, fire=lambda scope, p, target, confirm: {"summary": "noted"})
    r = Rig(tmp_path, extra={"note_it": spec})
    r.ck.register(ProviderSpec("ext:notes", "line", "Notes", "feed", lambda ctx: Read(value={"lines": []}),
                               verbs=("note_it",)))
    return r


def _fire_note(r) -> dict:
    from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, dispatch

    return dispatch(registry=r.reg, cockpit=r.ck, scope=r.src.write_scope, credential=CRED, install_class=INSTALL_CLASS,
                    route="cockpit", req={"verb": "note_it", "panel_id": "ext:notes", "params": {"text": "hi"},
                                          "idempotency_key": "k1"})


def test_a_downstream_verb_that_declares_nothing_touches_every_panel(tmp_path: Path) -> None:
    r = _downstream_rig(tmp_path)
    assert r.reg.is_downstream("note_it")
    assert _fire_note(r)["touched"] == r.ck.panel_ids              # a write never under-reports what it changed


def test_a_downstream_verb_that_declares_its_panels_touches_those(tmp_path: Path) -> None:
    r = _downstream_rig(tmp_path)
    r.reg.add_affects("note_it", ["health"])
    got = _fire_note(r)["touched"]
    assert got == [p for p in r.ck.panel_ids if p in {"ext:notes", "health"}]


def test_a_downstream_verbspec_that_declares_affects_touches_those(tmp_path: Path) -> None:
    from levain.cockpit.verbs import VerbSpec

    r = _downstream_rig(tmp_path)
    own = dataclasses.replace(r.reg.get("note_it"), affects=("health",))
    assert isinstance(own, VerbSpec)
    assert r.reg.touched(r.ck, own, "ext:notes") == [p for p in r.ck.panel_ids if p in {"ext:notes", "health"}]


def test_a_downstream_dry_run_carries_no_touched_and_fires_nothing(tmp_path: Path) -> None:
    from levain.cockpit import ProviderSpec, Read
    from levain.cockpit.verbs import VerbSpec
    from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, Rig, dispatch

    fired: list = []
    spec = VerbSpec("note_it", "note", "records a note", "panel", fields=("text",), floor="C1",
                    tier_fn=lambda *a: "C1", idempotent=True,
                    fire=lambda scope, p, target, confirm: fired.append(p) or {"touched": ["health"]})
    r = Rig(tmp_path, extra={"note_it": spec})
    r.ck.register(ProviderSpec("ext:notes", "line", "Notes", "feed", lambda ctx: Read(value={"lines": []}),
                               verbs=("note_it",)))
    out = dispatch(registry=r.reg, cockpit=r.ck, scope=r.src.write_scope, credential=CRED, install_class=INSTALL_CLASS,
                   route="cockpit", req={"verb": "note_it", "panel_id": "ext:notes", "params": {"text": "hi"},
                                         "dry_run": True})
    assert out["dry_run"] is True and "touched" not in out and fired == []


def _note(ck, result) -> "frozenset[str] | None":
    from types import SimpleNamespace

    from levain.web_server import _Handler

    me = SimpleNamespace(server=SimpleNamespace(cockpit=ck), _write_touched=None)
    _Handler._note_fired(me, result)
    return me._write_touched


def test_the_server_believes_a_touched_only_for_registered_panels(ck) -> None:
    assert _note(ck, {"touched": ["health", "no-such-panel"]}) == frozenset({"health"})
    assert _note(ck, {"touched": ["no-such-panel"]}) is None       # the global drop
    assert _note(ck, {"touched": [3, None]}) is None
    assert _note(ck, {"touched": []}) is None
    assert _note(ck, {"dry_run": True, "touched": ["health"]}) == frozenset()
