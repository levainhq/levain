"""Coordinator C6: the thread budget gauge (T6) and away = UNDECIDED + the morning card (T3), pinned to what the C6 lane's
scratch runs showed. Before C6 (run 1): a worker session made 7 tool calls under an envelope whose budget said 3, a
second and a third worker launch for the thread both activated pinned to it, nothing metered the thread, nothing asked
for a SIGN, and `levain coord morning` / `levain coord away` did not exist. Keys, authority log, repo, envelopes and Q13's
counter are real (the C2 tests' fixtures); the store clock is fixed. Synthetic words."""
from __future__ import annotations

import io
import json
import sqlite3
from argparse import Namespace
from datetime import timedelta

import pytest

from levain.consent import ConsentQueue, limits
from levain.consent import away as consent_away
from levain.consent.queue import AnswerRefused
from levain.consent.record import RULING_NAMESPACE, ruling_message
from levain.coordinator import budget, lease, morning, review
from levain.coordinator.cli import run_coord
from levain.coordinator.store import CoordError, Part
from tests.test_consent_queue import Clock, Log
from tests.test_coordinator_envelope import NOW, _lease, ctx, keys, repo, snapshot  # noqa: F401 - fixtures
from tests._h4 import h4_home


class Night:
    """A thread under a structured envelope whose budget allows ``calls`` tool calls, and one worker launch pinned to
    it (``sess-w1``), in one scratch consent directory."""

    def __init__(self, c, calls=3, **budget_over):
        self.c, self.store = c, c.store
        b = {"tool_calls": calls, "tokens": 1_000_000, "dollars_cents": 500, "hours": 2, **budget_over}
        d, _fresh, self.body, _signed = c.structured("tidy the readme wording and land it", budget=b)
        self.drop = d
        (self.thread,) = self.store.split(d.drop_id, [Part("work", "tidy")])
        self.T = self.thread.thread_id
        self.token = _lease(self.store)
        self.n = 0
        ln, why = self.leg()
        assert why is None and ln.pinned_envelope_id == self.body["envelope_id"]

    def leg(self):
        self.n += 1
        w = self.store.launch_request(self.token, harness="claude", threads=[self.T])
        return self.store.launch_activate(w.launch_id, harness_session_id=f"sess-w{self.n}")

    def calls(self, k, session="sess-w1"):
        for i in range(k):
            v = limits.count_call(self.store.directory, "claude_code", session, {"tool_use_id": f"{session}-{i}",
                                                                                  "cwd": self.c.path},
                                  tool_name="Bash", tool_input={"command": f"echo {i}"})
            assert v.state in ("ok", "crossed")

    def gauge(self):
        with sqlite3.connect(self.store.path) as conn:
            return budget.gauge(conn, self.store.directory, self.T)

    def signs(self):
        return self.c.rows("SELECT ref_store, ref_id FROM replies WHERE kind = 'sign'")

    def waiting(self):
        return review.waiting_on_you(self.store, sign_open=lambda _r: None, answer_open=lambda _r: None)

    def extension(self, key="laptop", **budget_over):
        """The body `levain envelope extend` writes (the head as narrowed, a bigger budget), signed by ``key``."""
        head = self.store.head(self.T)
        b = {**head.body["budget"], **budget_over}
        t = self.store.thread(self.T)
        return self.c.worded(self.drop, t, key, supersedes={"envelope_id": head.envelope_id, "digest": head.digest},
                             where=head.body["where"], lands=head.body["lands"], budget=b)


@pytest.fixture
def night(ctx):  # noqa: F811
    return Night(ctx)


def test_a_thread_past_its_budget_starts_no_new_leg_and_asks_once_for_a_sign(night):
    """Run 1 reproduced: 7 calls under a budget of 3, then new worker launches for the thread. Now the second leg is
    refused in its activation transaction, written failed (budget crossed), and ONE SIGN to extend is filed."""
    night.calls(7)
    g = night.gauge()
    assert g.used["tool_calls"] == 7 and g.crossed == ("tool_calls",) and g.sessions == (("claude_code", "sess-w1"),)
    ln, why = night.leg()
    assert ln.state == "failed" and ln.cause == lease.BUDGET_CROSSED and ln.pinned_envelope_id is None
    assert "has used its budget (tool_calls 7 of 3): new steps stop; a SIGN extends it" in why
    ref = f"{night.T}/{night.body['envelope_id']}"
    assert night.signs() == [("budget", ref)]
    assert night.leg()[1] is not None and night.signs() == [("budget", ref)]        # a second crossing: the same SIGN
    (rec,) = night.waiting()["threads"][night.T]
    assert rec["kind"] == "sign" and rec["ref_id"] == ref
    # a budget stop is not a seat that died (the dead-seat feed and the wake backoff read this one rule)
    with sqlite3.connect(night.store.path) as conn:
        assert lease.launch_death(conn, lease.launch(conn, ln.launch_id)) is False


def test_under_the_budget_a_new_leg_starts(night):
    night.calls(2)
    ln, why = night.leg()
    assert why is None and ln.state == "active" and night.signs() == []


def test_the_coordinator_cannot_extend_its_own_budget_and_only_his_signature_does(night):
    """The test that tries. Past the budget, the coordinator (a) signs the extension body with its own key, (b) tries an
    attenuation that would raise the budget, (c) deletes the SIGN record. Each is refused, and no new leg starts. Then
    the operator's enrolled key signs the same body through the store's one signer path, and the next leg starts."""
    night.calls(5)
    assert night.leg()[1] is not None
    before = night.c.rows("SELECT envelope_id FROM envelopes")
    _b, signed = night.extension("stranger", tool_calls=50)                 # never enrolled: the coordinator's key
    with pytest.raises(CoordError, match="not an enrolled key"):
        night.c.accept(signed)
    with pytest.raises(CoordError, match="kill, stop_earlier or drop_effect"):
        night.store.attenuate(night.body["envelope_id"], "budget", "tool_calls=50", thread_id=night.T)
    raw = sqlite3.connect(night.store.path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.Error, match="append-only"):
            raw.execute("DELETE FROM replies WHERE kind = 'sign'")
    finally:
        raw.close()
    assert night.c.rows("SELECT envelope_id FROM envelopes") == before
    assert night.gauge().crossed == ("tool_calls",) and night.leg()[1] is not None
    # his signature: the same extension, his laptop key
    body, signed = night.extension("laptop", tool_calls=50)
    night.c.accept(signed)
    g = night.gauge()
    assert g.envelope_id == body["envelope_id"] and g.used["tool_calls"] == 5 and g.crossed == ()   # usage is the thread's
    ln, why = night.leg()
    assert why is None and ln.pinned_envelope_id == body["envelope_id"]
    assert night.waiting()["threads"] == {}                                   # the superseded head's SIGN closed


def test_q13_stays_the_gate(night):
    """Extending the thread budget changes nothing Q13 holds: a session past its own Q13 limit stays crossed."""
    night.calls(5)
    snap = limits.snapshot(night.store.directory)
    night.c.accept(night.extension("laptop", tool_calls=500)[1])
    assert limits.snapshot(night.store.directory) == snap
    v = limits.count_call(night.store.directory, "claude_code", "sess-w1", {"tool_use_id": "x", "cwd": night.c.path},
                          tool_name="Bash", tool_input={"command": "x"}, limits={"calls": 3})
    assert v.state == "crossed" and v.kind == "calls"


def test_a_zero_budget_is_crossed_at_once_and_dollars_never_cross(ctx):  # noqa: F811
    zero = Night.__new__(Night)
    zero.c, zero.store = ctx, ctx.store
    d, _f, body, _s = ctx.structured("zero tokens", budget={"tool_calls": 10, "tokens": 0, "dollars_cents": 0,
                                                            "hours": 1})
    (t,) = ctx.store.split(d.drop_id, [Part("work", "z")])
    zero.T, zero.token, zero.n = t.thread_id, _lease(ctx.store), 0
    ln, why = zero.leg()
    assert ln.state == "failed" and "tokens 0 of 0" in why and "dollars" not in why


def test_an_unreadable_q13_store_stops_new_legs(night):
    night.calls(1)
    (night.store.directory / "limits.db").write_bytes(b"not a database" * 100)
    g = night.gauge()
    assert g.unreadable is not None and "cannot be metered" in g.refusal
    assert night.leg()[0].state == "failed"


def test_a_session_q13_has_not_seen_is_named_not_hidden(night):
    g = night.gauge()
    assert g.uncounted == (("claude_code", "sess-w1"),) and g.used["tool_calls"] == 0
    assert "sess-w1" in morning.render(morning.card(night.store))


# --- T3: away = UNDECIDED, and the morning card --------------------------------------------------------------------

@pytest.fixture
def away_night(night, keys, tmp_path):  # noqa: F811
    """``night`` plus what `levain init` writes for both agents (settings naming a cockpit with an enrolled key) and a
    consent queue in the same directory on a clock the test moves."""
    (tmp_path / "auth").mkdir()
    lg = Log(tmp_path / "auth", keys)
    lg.enrol("laptop", "laptop-presence")
    for name in ("claude_code.json", "codex.json"):
        (night.store.directory / name).write_text(json.dumps({"cockpit_data_root": str(tmp_path / "auth"),
                                                              "away_mode": False, "away_hold_seconds": 0}))
    night.clock = Clock(NOW)
    night.queue = ConsentQueue(night.store.directory, authority=lg.source, clock=night.clock, home=h4_home())
    night.keys = keys
    return night


def _undecided(n):
    req = n.queue.file(agent="claude_code", agent_session_id="sess-w1", tool_name="Bash",
                       tool_input={"command": "git push origin main"}, cwd=n.c.path, hold=timedelta(minutes=1))
    n.clock.now = NOW + timedelta(minutes=2)
    assert n.queue.mark_undecided(req.id).state == "undecided"
    return req


def test_the_coordinator_sets_away_on_from_his_word_and_can_never_leave_it_or_rule(away_night):
    """The test that tries (T3). Run 2: the coordinator read "closing my eyes" as away. It sets away ON (through the
    operator's own path) and files a KNOW; it then tries to rule on the UNDECIDED with its own key, to leave away with
    its own key, and to leave unsigned. Each is refused; the request stays undecided and away stays on."""
    n = away_night
    note, _ = n.store.accept_drop("note: closing my eyes for the night", surface="cli")
    n.store.split(note.drop_id, [Part("note", "closing my eyes")])
    with pytest.raises(CoordError):
        morning.away_from_drop(n.store, note.drop_id, token=n.token + 99)        # not the live lease: nothing set
    assert morning.card(n.store)["any_away"] is False
    got = morning.away_from_drop(n.store, note.drop_id, token=n.token)
    assert got["marked"] == ["claude_code", "codex"] and got["skipped"] == {}
    req = _undecided(n)
    stranger = n.keys["stranger"]
    rp = n.queue.ruling_payload(req.id, "allow", stranger.fp)
    with pytest.raises(AnswerRefused, match="not an enrolled key"):
        n.queue.answer_late(req.id, rp, stranger.sign(ruling_message(rp), RULING_NAMESPACE))
    with pytest.raises(ValueError, match="not an enrolled key"):
        consent_away.set_away(n.store.directory, "claude_code", False, signing_key=stranger.path)
    with pytest.raises(ValueError, match="takes a signature"):
        consent_away.set_away(n.store.directory, "claude_code", False)
    assert n.queue.get(req.id)[1] == "undecided"
    assert not [name for name in dir(morning) if name.startswith(("leave", "rule", "resolve", "off"))]
    c = morning.card(n.store)
    assert c["any_away"] and c["away"]["claude_code"]["away"] and c["away"]["codex"]["away"]
    (u,) = c["undecided"]
    assert (u["request_id"], u["gap"], u["thread_id"]) == (req.id, 1, n.T)
    assert u["rule"].startswith("levain consent rule 1 allow|deny --agent claude_code --session sess-w1")
    (k,) = [i for i in c["review"] if i["feed"] == "know"]
    assert "read your drop" in k["text"] and "closing my eyes" in k["text"]


def test_away_with_no_settings_names_why_and_sets_nothing(night):
    note, _ = night.store.accept_drop("closing my eyes", surface="cli")
    night.store.split(note.drop_id, [Part("note", "eyes")])
    with pytest.raises(CoordError, match="away was not set for any agent"):
        morning.away_from_drop(night.store, note.drop_id, token=night.token)
    assert night.c.rows("SELECT COUNT(*) FROM review_items WHERE feed = 'know'") == [(0,)]


def test_the_card_shows_a_crossed_budget_first_with_its_open_sign(night):
    night.calls(4)
    night.leg()
    c = morning.card(night.store)
    (b,) = c["budgets"]
    assert b["refusal"] and b["crossed"] == ["tool_calls"] and b["sign"]["open"] is True
    text = morning.render(c)
    assert "CROSSED: new steps stop" in text and f"levain envelope extend {night.T}" in text
    assert "dollars not metered" in text


def _coord_args(store, cmd, **kw):
    return Namespace(coord_cmd=cmd, queue_dir=store.directory, json=kw.pop("json", False), **kw)


def test_the_cli_views(night):
    night.calls(4)
    out = io.StringIO()
    assert run_coord(_coord_args(night.store, "budget", thread=night.T), out) == 0
    assert "tool_calls 4 of 3" in out.getvalue() and "new steps stop" in out.getvalue()
    out = io.StringIO()
    assert run_coord(_coord_args(night.store, "morning", json=True), out) == 0
    assert json.loads(out.getvalue())["budgets"][0]["crossed"] == ["tool_calls"]


# --- C6 L3 r1 fix round (head rulings B1, B2, C6-3, A1, E1, U1) ----------------------------------------------------

def test_b1_a_multi_thread_worker_is_refused_at_request_and_at_activation(night):
    """L3 r1 codex: a worker holding [crossed T, T2] activated with no budget check, no SIGN and no meter (only a pinned
    one-thread worker was metered). C0 §3.4 gives a launch holding several threads no envelope authority, so it has no
    budget semantics: such a worker is refused when asked for, and one already pending is refused at activation."""
    night.calls(7)
    _d2, t2 = night.c.plain_thread("a second thread")
    with pytest.raises(CoordError, match="one thread"):
        night.store.launch_request(night.token, harness="claude", threads=[night.T, t2.thread_id])
    # one already pending (asked for before this rule): written as a store of the old code would hold it
    with night.store._write() as (conn, now):
        old = lease._pending(conn, now, token=night.token, harness="claude", role="worker",
                             threads=[night.T, t2.thread_id], model=None, cwd=None, cause="requested")
    ln, why = night.store.launch_activate(old.launch_id, harness_session_id="sess-multi")
    assert ln.state == "failed" and why is not None and "one thread" in why
    assert ln.cause == lease.SEVERAL_THREADS
    with sqlite3.connect(night.store.path) as conn:
        assert lease.launch_death(conn, lease.launch(conn, ln.launch_id)) is False


def test_b2_a_crossed_thread_is_refused_before_the_spawn(night):
    """L3 r1 complement MED: the budget was read only at activation, after `claude --bg` had started a session. The
    spawn claim (the one CAS every spawner takes before it spawns) now refuses a crossed thread's worker: no spawn
    claim row, the launch written failed (budget crossed) and the SIGN filed, all before anything starts."""
    night.calls(7)
    w = night.store.launch_request(night.token, harness="claude", threads=[night.T])
    ln, why = night.store.launch_claim_spawn(w.launch_id, 4242)
    assert why is not None and "has used its budget" in why
    assert night.c.rows(f"SELECT COUNT(*) FROM launch_spawns WHERE launch_id = '{w.launch_id}'") == [(0,)]
    assert night.store.launch(w.launch_id).state == "failed"
    assert night.store.launch(w.launch_id).cause == lease.BUDGET_CROSSED
    assert night.signs() == [("budget", f"{night.T}/{night.body['envelope_id']}")]
    # under the budget the claim is the same as before
    other = Night(night.c, calls=100)
    w2 = other.store.launch_request(other.token, harness="claude", threads=[other.T])
    assert other.store.launch_claim_spawn(w2.launch_id, 4243)[1] is None


def test_c6_3_echo_refuses_a_budget_that_omits_a_measured_key(night, monkeypatch):
    from levain.coordinator.cli import run_envelope_echo

    def args(b):
        return Namespace(threads=[night.T], where=[f"{night.c.path}=."], land=[], stop="2h", budget=b, output=None,
                         force=False, queue_dir=night.store.directory, json=False)

    with pytest.raises(CoordError) as e:
        run_envelope_echo(args("tool_calls=50"), io.StringIO())
    assert "--budget leaves out tokens, hours, which the budget gauge measures" in str(e.value)
    with pytest.raises(CoordError, match="hours"):
        run_envelope_echo(args("tool_calls=50,tokens=0"), io.StringIO())


def _extend(night, monkeypatch, b):
    from levain.coordinator import cli

    monkeypatch.setattr(cli, "_tick", lambda: NOW)
    return cli.run_envelope_extend(Namespace(thread=night.T, budget=b, output=None, force=False,
                                             queue_dir=night.store.directory, json=False), io.StringIO())


def test_e1_extend_refuses_a_lower_value_and_needs_a_greater_one(night, monkeypatch):
    with pytest.raises(CoordError, match="below"):
        _extend(night, monkeypatch, "tool_calls=1")
    with pytest.raises(CoordError, match="below"):
        _extend(night, monkeypatch, "tool_calls=50,hours=1")
    with pytest.raises(CoordError, match="greater"):
        _extend(night, monkeypatch, "tool_calls=3,hours=2")
    assert _extend(night, monkeypatch, "tool_calls=50") == 0


def test_u1_a_limits_store_with_no_sessions_table_reads_unreadable_never_crossed(night):
    night.calls(1)
    p = night.store.directory / "limits.db"
    p.unlink()
    sqlite3.connect(p).execute("CREATE TABLE other (x)").connection.commit()
    g = night.gauge()
    assert g.unreadable is not None and "no sessions table" in g.unreadable and g.crossed == ()
    assert "no sessions table" in g.refusal
    text = morning.render(morning.card(night.store))
    assert "UNREADABLE" in text and "no sessions table" in text and "CROSSED" not in text
    out = io.StringIO()
    assert run_coord(_coord_args(night.store, "budget", thread=night.T), out) == 0
    assert "UNREADABLE" in out.getvalue() and "0 of 3" not in out.getvalue()
    # a leg is refused, and no SIGN to extend is filed: no extension unblocks an unreadable meter
    assert night.leg()[0].state == "failed" and night.signs() == []


def test_a1_away_says_exactly_which_agents_were_set_and_which_skipped(away_night, monkeypatch):
    n = away_night
    note, _ = n.store.accept_drop("closing my eyes", surface="cli")
    n.store.split(note.drop_id, [Part("note", "eyes")])
    real = consent_away.set_away

    def flaky(qdir, agent, on, *a, **k):
        if agent == "codex":
            raise OSError("codex.json: permission denied")
        return real(qdir, agent, on, *a, **k)

    monkeypatch.setattr(consent_away, "set_away", flaky)
    got = morning.away_from_drop(n.store, note.drop_id, token=n.token)
    assert got["marked"] == ["claude_code"] and "permission denied" in got["skipped"]["codex"]
    (k,) = [str(i.body["text"]) for i in review.list_items(n.store) if i.feed == "know"]
    assert "every" not in k and "NOT set for codex" in k and "permission denied" in k
    # the CLI view of that result (the CLI's own store runs on the wall clock, past this test's lease: its result is
    # handed in)
    monkeypatch.setattr(morning, "away_from_drop", lambda *_a, **_k: got)
    out = io.StringIO()
    assert run_coord(_coord_args(n.store, "away", drop_id=note.drop_id, token=n.token), out) == 0
    text = out.getvalue()
    assert "away ON for claude_code only" in text and "NOT set for codex" in text and "permission denied" in text
    assert "every" not in text


@pytest.mark.parametrize("step", ["claim", "activate"])
def test_a_dead_asker_on_a_crossed_thread_is_refused_for_its_lease_and_files_no_sign(night, step):
    """C6 L3 r2 (consensus LOW): the budget was read BEFORE the lease's liveness, so a pending worker whose asker lost
    the lease, on a crossed thread, was written `budget crossed` and filed a SIGN to extend, where it is `lease lost
    before active` and nothing to sign (red on 81dfd173). The claim and the activation both read the lease first."""
    # the asker's lease runs out before its pending launch does (a take, a release and a lapse void or expire pending
    # launches; a lease past its until that nobody has lapsed yet leaves this one pending): a one-minute hold, a
    # LAUNCH_TTL launch, the clock two minutes on
    assert lease.LAUNCH_TTL > timedelta(minutes=2)
    token = night.store.coord_take(hold=timedelta(minutes=1))
    night.calls(7)
    w = night.store.launch_request(token, harness="claude", threads=[night.T])
    night.store._clock = lambda: NOW + timedelta(minutes=2)
    if step == "claim":
        ln, why = night.store.launch_claim_spawn(w.launch_id, 4242)
    else:
        ln, why = night.store.launch_activate(w.launch_id, harness_session_id="sess-dead")
    assert ln.state == "failed" and ln.cause == "lease lost before active", (ln.state, ln.cause, why)
    assert night.signs() == []
