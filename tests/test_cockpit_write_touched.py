"""levain#157 W: a cockpit verb answers with what it touched (``touched``, and ``row`` where the write held the record), and
only those panels' cached reads go cold. The HTTP tests drive the same snooze ``test_cockpit_k2a`` fires over a real server."""
from __future__ import annotations

import dataclasses
import json
import re
import threading
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
    assert mine and mine[-1]["received_at"] and mine[-1]["ts"] >= mine[-1]["received_at"]


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
    assert ck.panel("tray", credential_class="token") is after  # the pre-write owner's result was never stored


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
