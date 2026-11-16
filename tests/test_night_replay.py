"""ND-9a (board levain#40): the offline replay harness for the night desk's deterministic layers, its corpus and its
oracle. The model-in-the-loop (fresh and resident desk) runs are levain#18; here a fake runner stands in for the
model and a fake transport for the push."""
from __future__ import annotations

import copy
import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "night_replay.py"
CORPUS = ROOT / "tests" / "drills" / "night_corpus.json"
ORACLE = ROOT / "tests" / "drills" / "night_oracle.json"
CLASSES = {"forged", "real", "ambiguous", "hostile_body", "hostile_reader_field"}


def _load():
    spec = importlib.util.spec_from_file_location("night_replay", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def replay():
    return _load()


@pytest.fixture(scope="module")
def docs():
    return json.loads(CORPUS.read_text("utf-8")), json.loads(ORACLE.read_text("utf-8"))


def test_corpus_holds_at_least_twenty_events_across_the_five_classes(docs):
    corpus, oracle = docs
    events = corpus["events"]
    assert len(events) >= 20
    assert {e["class"] for e in events} == CLASSES
    ids = [e["id"] for e in events]
    assert len(set(ids)) == len(ids)
    assert set(oracle["expect"]) == set(ids)


def test_the_committed_oracle_and_the_code_agree(replay, docs):
    corpus, oracle = docs
    observed = replay.replay_all(corpus)
    assert replay.disagreements(corpus, oracle, observed) == []


def test_main_runs_offline_and_prints_the_table(replay, docs, monkeypatch, capsys):
    def no_network(*a, **kw):
        raise AssertionError("the harness opened a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    code = replay.main(["--corpus", str(CORPUS), "--oracle", str(ORACLE)])
    out = capsys.readouterr().out
    assert code == 0
    assert f"{len(docs[0]['events'])} events" in out and "0 disagreements" in out


def test_a_wrong_oracle_entry_is_a_row_in_the_table_and_a_failing_exit(replay, docs, tmp_path, capsys):
    corpus, oracle = docs
    bad = copy.deepcopy(oracle)
    bad["expect"]["M02"]["trust"] = "authenticated"      # a forged mail the oracle wrongly trusts
    del bad["expect"]["S01"]                             # an event nobody wrote an expectation for
    path = tmp_path / "bad_oracle.json"
    path.write_text(json.dumps(bad), "utf-8")
    code = replay.main(["--corpus", str(CORPUS), "--oracle", str(path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "M02" in out and "trust" in out and "authenticated" in out and "untrusted" in out
    assert "S01" in out and "no oracle entry" in out
    assert "2 disagreements" in out


def test_a_door_that_trusts_a_forged_mail_shows_in_the_table(replay, docs, monkeypatch):
    """Mutation check: if the mail door took the From on faith, the forged rows disagree with the oracle."""
    corpus, oracle = docs
    monkeypatch.setattr(replay.events, "_effective_trust", lambda ceiling, kind, fields: ceiling)
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    trust_rows = [r for r in rows if r[2] == "trust"]
    assert {r[0] for r in trust_rows} == {"M02", "M03", "M04", "M05", "M06", "M07", "M09"}
    assert all(r[3] == "untrusted" and r[4] == "authenticated" for r in trust_rows)


def test_a_parser_that_repairs_shows_in_the_table(replay, docs, monkeypatch):
    corpus, oracle = docs
    real = replay.sealed.parse_reader_output

    def lax(raw):
        return real(raw.replace("```json\n", "").replace("\n```", "")) if isinstance(raw, str) else real(raw)

    monkeypatch.setattr(replay.sealed, "parse_reader_output", lax)
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    assert [r[0] for r in rows] == ["R09"]


def test_the_fake_transport_takes_the_alerts_and_nothing_else_does(replay, docs):
    corpus, oracle = docs
    sent: list = []
    observed = replay.replay_all(corpus, transport=sent)
    alerting = [eid for eid, want in oracle["expect"].items() if want.get("alert")]
    assert alerting and len(sent) == len(alerting)   # one push per untrusted event that opened an incident
    assert sorted(eid for eid, got in observed.items() if got.get("pinged")) == sorted(alerting)


def test_the_fake_runner_returns_the_corpus_output_and_nothing_else(replay):
    run = replay.FakeRunner()
    assert run({"id": "R01", "layer": "reader", "output": "x"}) == "x"
    assert len(json.loads(run({"id": "R08", "layer": "reader", "output_text_len": 7}))["untrusted_text"]) == 7


# --- the L3 fix round -----------------------------------------------------------------------------------------------
def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), "utf-8")
    return str(path)


def _mini(replay, events_):
    """A small corpus on the committed sensors; the oracle is whatever the test writes."""
    corpus = json.loads(CORPUS.read_text("utf-8"))
    corpus["events"] = events_
    return corpus


_UT_POST = {"id": "X01", "layer": "sensor", "class": "forged", "sensor": "probe-ut", "kind": "site_status",
            "fields": {"subject": "taosrv", "state": "down"}}


def test_an_alert_that_errors_is_an_alert_error_on_a_posted_event_not_a_refusal(replay, monkeypatch):
    from levain.desk.store import CoordError

    def boom(*a, **kw):
        raise CoordError("the store is busy")

    monkeypatch.setattr(replay.events, "alert_untrusted", boom)
    got = replay.replay_all(_mini(replay, [_UT_POST]))["X01"]
    assert got["outcome"] == "posted" and got["alert"] is True and got["pinged"] is False
    assert "the store is busy" in got["alert_error"]


def test_every_alert_of_a_multi_post_event_is_kept(replay):
    twice = dict(_UT_POST, times=2)      # the first post opens the incident, the replay of the same seq does not
    sent: list = []
    got = replay.replay_all(_mini(replay, [twice]), transport=sent)["X01"]
    assert got["outcome"] == "posted" and got["alert"] is True and got["pinged"] is True and len(sent) == 1


@pytest.mark.parametrize("bad_corpus", [
    [],                                                  # not an object
    {"events": {}},                                      # no sensors, events not a list
    "no_sensors",
    "events_a_dict",
    "event_not_an_object",
    "event_without_id",
    "event_unknown_layer",
    "sensor_event_without_fields",
    "reader_event_without_output",
])
def test_a_structurally_bad_corpus_exits_2_with_one_line_never_a_traceback(replay, docs, tmp_path, capsys, bad_corpus):
    corpus, oracle = docs
    doc = copy.deepcopy(corpus)
    if bad_corpus == "no_sensors":
        del doc["sensors"]
    elif bad_corpus == "events_a_dict":
        doc["events"] = {"M01": {}}
    elif bad_corpus == "event_not_an_object":
        doc["events"][0] = "M01"
    elif bad_corpus == "event_without_id":
        del doc["events"][0]["id"]
    elif bad_corpus == "event_unknown_layer":
        doc["events"][0]["layer"] = "telepathy"
    elif bad_corpus == "sensor_event_without_fields":
        next(e for e in doc["events"] if e["layer"] == "sensor").pop("fields")
    elif bad_corpus == "reader_event_without_output":
        next(e for e in doc["events"] if e["layer"] == "reader").pop("output")
    else:
        doc = bad_corpus
    code = replay.main(["--corpus", _write(tmp_path, "c.json", doc), "--oracle", str(ORACLE)])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.startswith("night_replay: corpus ") and len(captured.err.strip().splitlines()) == 1


@pytest.mark.parametrize("bad_oracle", [[], {"expect": []}, {"expect": {"M01": "posted"}}, {"expect": {"M01": []}}])
def test_a_structurally_bad_oracle_exits_2_with_one_line_never_a_traceback(replay, tmp_path, capsys, bad_oracle):
    code = replay.main(["--corpus", str(CORPUS), "--oracle", _write(tmp_path, "o.json", bad_oracle)])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.startswith("night_replay: oracle ") and len(captured.err.strip().splitlines()) == 1


def test_a_duplicate_event_id_in_the_corpus_or_the_oracle_is_refused(replay, docs, tmp_path, capsys):
    corpus, _ = docs
    dup = copy.deepcopy(corpus)
    dup["events"].append(copy.deepcopy(dup["events"][0]))
    assert replay.main(["--corpus", _write(tmp_path, "c.json", dup), "--oracle", str(ORACLE)]) == 2
    assert "appears twice" in capsys.readouterr().err
    text = ORACLE.read_text("utf-8")
    twice = text.replace('"M01":', '"M02": {"outcome": "posted"}, "M01":', 1)     # a second M02, as the file says it
    assert replay.main(["--corpus", str(CORPUS), "--oracle", _write(tmp_path, "o.json", twice)]) == 2
    assert "duplicate key 'M02'" in capsys.readouterr().err


def test_a_key_the_oracle_names_that_was_not_observed_is_a_disagreement(replay):
    corpus = _mini(replay, [{"id": "R01", "layer": "reader", "class": "real",
                             "output": json.dumps({"category": "other", "sender_as_authenticated": "consistent",
                                                   "instructions_present": "no", "untrusted_text": "hi"})}])
    oracle = {"expect": {"R01": {"outcome": "accepted", "alert": None}}}      # a reader result has no alert key
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    assert rows == [("R01", "real", "alert", "None", "missing")]


def test_a_refusal_for_the_wrong_reason_is_a_disagreement(replay, docs):
    corpus, oracle = docs
    wrong = copy.deepcopy(oracle)
    wrong["expect"]["S07"]["why"] = "sensor refused"            # S07 is refused, but for its enum case, not its sensor
    wrong["expect"]["S12"]["refusal"] = "EventRefused"          # and S12 is the conflict
    rows = replay.disagreements(corpus, wrong, replay.replay_all(corpus))
    assert [(r[0], r[2]) for r in rows] == [("S07", "why"), ("S12", "refusal")]


def test_every_refused_row_of_the_oracle_names_its_reason(docs):
    corpus, oracle = docs
    refused = [eid for eid, want in oracle["expect"].items() if want.get("outcome") == "refused"]
    assert refused and all(oracle["expect"][eid].get("why") and oracle["expect"][eid].get("refusal")
                           for eid in refused)


def test_an_unregistered_sensor_or_a_raising_event_is_a_row_and_the_drill_goes_on(replay, monkeypatch):
    ghost = {"id": "G01", "layer": "sensor", "class": "forged", "sensor": "ghost", "kind": "site_status",
             "fields": {"subject": "taosrv", "state": "down"}}          # no credential key, no such sensor
    corpus = _mini(replay, [ghost, dict(_UT_POST, id="G02"), dict(_UT_POST, id="G03")])
    real = replay.events.post

    def flaky(store, sensor_id, *a, **kw):
        if kw.get("untrusted_text") == "boom":
            raise RuntimeError("the door fell over")
        return real(store, sensor_id, *a, **kw)

    corpus["events"][1]["untrusted_text"] = "boom"
    monkeypatch.setattr(replay.events, "post", flaky)
    oracle = {"expect": {k: {"outcome": "posted"} for k in ("G01", "G02", "G03")}}
    observed = replay.replay_all(corpus)
    assert observed["G01"]["outcome"] == "refused"
    assert observed["G02"]["outcome"] == "error" and "the door fell over" in observed["G02"]["error"]
    assert observed["G03"]["outcome"] == "posted"
    assert [(r[0], r[2]) for r in replay.disagreements(corpus, oracle, observed)] == [("G01", "outcome"),
                                                                                       ("G02", "error")]


# --- the L3 fix round 2: the drill must not lie about an error it saw -----------------------------------------------
def _boom_alert(replay, monkeypatch, message="the store is busy"):
    from levain.desk.store import CoordError

    def boom(*a, **kw):
        raise CoordError(message)

    monkeypatch.setattr(replay.events, "alert_untrusted", boom)


def test_an_alert_error_is_a_disagreement_even_when_the_oracle_is_silent_about_it(replay, monkeypatch, tmp_path, capsys):
    _boom_alert(replay, monkeypatch)
    corpus = _mini(replay, [_UT_POST])
    oracle = {"expect": {"X01": {"outcome": "posted", "alert": True}}}       # names nothing about the alert's outcome
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    assert [(r[0], r[2]) for r in rows] == [("X01", "alert_error")]
    assert "the store is busy" in rows[0][4]
    code = replay.main(["--corpus", _write(tmp_path, "c.json", corpus), "--oracle", _write(tmp_path, "o.json", oracle)])
    assert code == 1 and "1 disagreements" in capsys.readouterr().out


def test_an_alert_error_stays_a_row_when_the_oracle_names_that_very_error(replay, monkeypatch):
    _boom_alert(replay, monkeypatch)
    corpus = _mini(replay, [_UT_POST])
    got = replay.replay_all(corpus)
    oracle = {"expect": {"X01": {"outcome": "posted", "alert_error": got["X01"]["alert_error"]}}}
    assert [(r[0], r[2]) for r in replay.disagreements(corpus, oracle, got)] == [("X01", "alert_error")]


def test_a_raising_event_is_a_disagreement_whatever_the_oracle_names(replay, monkeypatch):
    def fell_over(*a, **kw):
        raise RuntimeError("the door fell over")

    monkeypatch.setattr(replay.events, "post", fell_over)
    corpus = _mini(replay, [_UT_POST])
    for want in ({}, {"outcome": "error"}):
        rows = replay.disagreements(corpus, {"expect": {"X01": want}}, replay.replay_all(corpus))
        assert [r[2] for r in rows] == ["error"] and "the door fell over" in rows[0][4]


@pytest.mark.parametrize("key", ["alert_error", "error"])
def test_an_oracle_expecting_an_error_the_event_did_not_produce_is_a_disagreement(replay, tmp_path, capsys, key):
    corpus = _mini(replay, [_UT_POST])
    oracle = {"expect": {"X01": {key: "boom"}}}
    got = replay.replay_all(corpus)
    assert key not in got["X01"]
    assert [(r[0], r[2], r[4]) for r in replay.disagreements(corpus, oracle, got)] == [("X01", key, "missing")]
    code = replay.main(["--corpus", _write(tmp_path, "c.json", corpus), "--oracle", _write(tmp_path, "o.json", oracle)])
    assert code == 1


def test_a_raising_event_is_one_row_even_when_the_oracle_names_other_keys(replay, monkeypatch):
    def fell_over(*a, **kw):
        raise RuntimeError("the door fell over")

    monkeypatch.setattr(replay.events, "post", fell_over)
    corpus = _mini(replay, [_UT_POST])
    oracle = {"expect": {"X01": {"outcome": "posted", "alert": True, "pinged": True}}}
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    assert [r[2] for r in rows] == ["error"]


def test_a_raising_event_absent_from_the_oracle_is_one_row_not_two(replay, monkeypatch):
    def fell_over(*a, **kw):
        raise RuntimeError("the door fell over")

    monkeypatch.setattr(replay.events, "post", fell_over)
    corpus = _mini(replay, [_UT_POST, dict(_UT_POST, id="X02")])
    oracle = {"expect": {"X02": {"outcome": "posted"}}}                      # valid, but names no X01
    rows = replay.disagreements(corpus, oracle, replay.replay_all(corpus))
    assert [(r[0], r[2]) for r in rows if r[0] == "X01"] == [("X01", "error")]


def test_an_empty_oracle_entry_exits_2_with_a_reason(replay, docs, tmp_path, capsys):
    corpus, _ = docs
    code = replay.main(["--corpus", _write(tmp_path, "c.json", corpus),
                        "--oracle", _write(tmp_path, "o.json", {"expect": {"M01": {}}})])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == "" and len(captured.err.strip().splitlines()) == 1
    assert captured.err.startswith("night_replay: oracle ") and "empty" in captured.err


def test_every_alert_error_of_a_multi_alert_event_is_kept_and_pinged_means_one_went_out(replay, monkeypatch):
    from types import SimpleNamespace
    from levain.desk.store import CoordError
    calls: list = []

    def first_goes_out_the_rest_fail(store, post, *, pinger):
        calls.append(post)
        if len(calls) == 1:
            pinger(None)                                  # the push itself, recorded by the fake transport
            return None
        raise CoordError(f"failure {len(calls)}")

    monkeypatch.setattr(replay.events, "alert_untrusted", first_goes_out_the_rest_fail)
    ev = SimpleNamespace(trust="untrusted", fields={}, untrusted_text=None)
    posts = [SimpleNamespace(event=ev, created=True, alert=True) for _ in range(3)]
    sent: list = []
    got = replay._posted(None, posts, None, sent)
    assert got["pinged"] is True and len(sent) == 1
    assert "failure 2" in got["alert_error"] and "failure 3" in got["alert_error"]


def test_pinged_is_false_when_every_alert_raised(replay, monkeypatch):
    _boom_alert(replay, monkeypatch)
    got = replay.replay_all(_mini(replay, [_UT_POST]))["X01"]
    assert got["pinged"] is False and "the store is busy" in got["alert_error"]


@pytest.mark.parametrize("which", ["corpus", "oracle"])
def test_an_empty_events_list_or_empty_expect_exits_2_with_a_reason(replay, docs, tmp_path, capsys, which):
    corpus, oracle = docs
    bad_corpus = dict(copy.deepcopy(corpus), events=[]) if which == "corpus" else corpus
    bad_oracle = {"expect": {}} if which == "oracle" else oracle
    code = replay.main(["--corpus", _write(tmp_path, "c.json", bad_corpus), "--oracle", _write(tmp_path, "o.json", bad_oracle)])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.startswith(f"night_replay: {which} ") and len(captured.err.strip().splitlines()) == 1
    assert "empty" in captured.err


def test_a_wholly_missing_observation_is_a_missing_row_not_a_crash(replay, docs):
    corpus, oracle = docs
    observed = replay.replay_all(corpus)
    del observed["M01"]
    rows = replay.disagreements(corpus, oracle, observed)
    assert rows == [("M01", next(e["class"] for e in corpus["events"] if e["id"] == "M01"),
                     "observation", "one", "missing")]


def test_a_deeply_nested_json_file_exits_2_like_any_bad_file(replay, tmp_path, capsys):
    deep = "[" * 100000 + "]" * 100000
    path = tmp_path / "c.json"
    path.write_text(deep, "utf-8")                  # raw text, so it really nests (a JSON-quoted string would not)
    code = replay.main(["--corpus", str(path), "--oracle", str(ORACLE)])      # whichever error this interpreter raises
    captured = capsys.readouterr()
    assert code == 2 and captured.err.startswith("night_replay: corpus ") and len(captured.err.strip().splitlines()) == 1


def test_a_recursion_error_from_the_json_parser_exits_2_like_any_bad_file(replay, tmp_path, capsys, monkeypatch):
    def too_deep(*a, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(replay.json, "loads", too_deep)      # the exception some interpreters raise for deep input
    path = tmp_path / "c.json"
    path.write_text("[]", "utf-8")
    code = replay.main(["--corpus", str(path), "--oracle", str(ORACLE)])
    captured = capsys.readouterr()
    assert code == 2 and captured.err.startswith("night_replay: corpus ") and len(captured.err.strip().splitlines()) == 1
    assert "recursion" in captured.err


def test_a_wrong_credential_and_an_unknown_sensor_get_the_same_refusal_by_design(replay, docs):
    """The door answers every refusal cause with one message so no probe learns which sensors exist; the oracle's
    S04 (wrong credential) and S05 (unregistered sensor) therefore name the same reason, and the same class."""
    corpus, oracle = docs
    observed = replay.replay_all(corpus)
    assert observed["S04"] == observed["S05"] == {"outcome": "refused", "refusal": "EventRefused",
                                                  "why": "sensor refused"}
    assert oracle["expect"]["S04"] == oracle["expect"]["S05"]
