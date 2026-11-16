"""levain#66 slice S4a (plan hgs4_plan_v2, tests T1-T9, T11-T13, T18, T23, T25): the pure half of host cards,
``levain/slot/egress_cards.py``, driven through its reducer by a fake world that answers each command the way root, the
queue and the supervisor would. No sudo, no pty, no network, no real key.

Every test that names the signed words reaches them through ``LEVAIN_SIGN_CHOICES`` / ``eg.option_text``; the exact
words are pinned in one place, ``tests/test_contracts_namespaces.py``, so a ruling on the wording is a one-constant
diff."""
from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.contracts import namespaces as NS
from levain.slot import egress_cards as C
from levain.slot import egress_grants as eg
from levain.slot import egress_ledger as EL
from levain.slot import egress_store as ES
from levain.slot import egress_verbs as EV
from levain.slot import egress_verify as EVY

ROOT = Path(__file__).resolve().parent.parent
SESSION = "s-0123456789abcdef"
STORE = "store-abc12345"
RP = "cockpit.example.ts.net"
FP = "SHA256:" + "A" * 43
SIG = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n"
T0 = 1_800_000_000.0


def at(wall: float) -> datetime:
    return datetime.fromtimestamp(wall, tz=timezone.utc)


def record_for(message: str, choice: str, **over) -> bytes:
    signed = eg.signed_bytes(message, choice)
    rec = {"signer_fp": FP, "public_key": "x", "namespace": NS.EGRESS_HOST_NAMESPACE,
           "message_sha256": hashlib.sha256(signed).hexdigest(), "signed_at": "2026-11-16T12:00:00.000000Z",
           "choice": choice, "signed_message": signed.decode()}
    rec.update(over)
    return json.dumps(rec).encode()


class World:
    """The fake world a flow runs in: a refusals log root reads 8 distinct hosts at a time from, root's live grant
    list, a sign queue of cards, a clock pair, and a record of every call the flow asked for."""

    def __init__(self, hosts=(), *, rp=RP, slot=3, phone_live=True):
        self.flow = C.CardFlow(session=SESSION, slot=slot, rp=rp)
        self.wall, self.mono = T0, 1000.0
        self.log: list[str] = list(hosts)                  # front.log lines, one host each
        self.live: dict[str, tuple[str, datetime]] = {}    # host -> (grant, expires): root's grants file
        self.store = STORE
        self.queue: dict[str, dict] = {}                   # rid -> {state, message, title, payload}
        self.open_elsewhere = False
        self.phone_live = phone_live
        self.calls: list[tuple] = []                       # ("poll", cursor) ("list",) ("grant", payload) ...
        self.pushes: list[C.Push] = []
        self.files: list[C.File] = []
        self.grant_answers: list[object] = []              # scripted: a refusal name, "timeout", or None for ok
        self.list_answer: object = None                    # None ok, else a refusal name
        self.poll_answer: object = None
        self.poll_cap: int | None = None                   # stop executing after this many polls
        self.rid_n = 0
        self.polls = 0

    # -- driving ---------------------------------------------------------------------------------------------------
    def step(self, event) -> None:
        self.flow, cmds = self.flow.advance(event, self.wall, self.mono)
        for cmd in cmds:
            self.run(cmd)

    def tick(self, dt: float = 0.0, *, mono_dt: float | None = None) -> None:
        self.wall += dt
        self.mono += dt if mono_dt is None else mono_dt
        self.step(C.Tick())

    def settle(self, n: int = 3) -> None:
        for _ in range(n):
            self.tick()

    def run(self, cmd) -> None:
        if isinstance(cmd, C.Poll):
            if self.poll_cap is not None and self.polls >= self.poll_cap:
                return
            self.polls += 1
            self.calls.append(("poll", cmd.cursor))
            if self.poll_answer is not None:
                self.step(C.RunFailed(C.REFUSALS, str(self.poll_answer)))
                return
            i, seen = int(cmd.cursor), []
            if i > len(self.log):
                self.step(C.RunFailed(C.REFUSALS, EV.ERR_CURSOR))
                return
            while i < len(self.log) and len(seen) < C.REFUSALS_HOSTS_MAX:
                if self.log[i] not in seen:
                    seen.append(self.log[i])
                i += 1
            self.step(C.Polled(tuple(seen), i))
        elif isinstance(cmd, C.ListGrants):
            self.calls.append(("list",))
            if self.list_answer is not None:
                self.step(C.RunFailed(C.GRANTS_LIST, str(self.list_answer)))
                return
            now = at(self.wall)
            entries = tuple((g, h, e, "s") for h, (g, e) in self.live.items() if e > now)
            self.step(C.Listed(self.store, entries))
        elif isinstance(cmd, C.Scan):
            self.calls.append(("scan",))
            self.step(C.Scanned(self.open_elsewhere or any(c["state"] == "open" and c["expires"] > self.wall
                                                           for c in self.queue.values())))
        elif isinstance(cmd, C.File):
            self.rid_n += 1
            rid = f"lv-{self.rid_n:032x}"
            self.files.append(cmd)
            self.queue[rid] = {"state": "open", "message": cmd.message, "title": cmd.title, "payload": None,
                                "expires": self.wall + cmd.ttl_s}
            self.step(C.Filed(rid, self.phone_live))
        elif isinstance(cmd, C.Push):
            self.pushes.append(cmd)
        elif isinstance(cmd, C.ReadAnswer):
            q = self.queue[cmd.rid]
            self.step(C.Answer(cmd.rid, q["state"], q["payload"]))
        elif isinstance(cmd, C.Deliver):
            self.calls.append(("grant", cmd.payload, cmd.start_by, self.wall))
            ans = self.grant_answers.pop(0) if self.grant_answers else None
            if ans == "timeout":
                self.step(C.RunFailed(C.GRANT, None))
            elif ans is not None:
                self.step(C.Delivered(str(ans)))
            else:
                g = eg.parse_signed(eg.signed_bytes(cmd.payload["message"], cmd.payload["choice"]))
                self.live[g.host] = (g.grant, g.expires)
                self.step(C.Delivered(None, {"grant": g.grant, "host": g.host, "expires": eg.stamp(g.expires),
                                             "choice": g.choice}))
        else:                                              # pragma: no cover
            raise AssertionError(cmd)

    # -- the operator's hands --------------------------------------------------------------------------------------
    def card(self, n: int = -1) -> str:
        return list(self.queue)[n]

    def tap(self, rid: str | None = None, choice: str = "s") -> None:
        rid = rid or self.card()
        q = self.queue[rid]
        q["state"], q["payload"] = "signed", C.read_signed(record_for(q["message"], choice), SIG.encode(), q["message"])

    def deny(self, rid: str | None = None) -> None:
        self.queue[rid or self.card()]["state"] = "denied"

    def host_of(self, rid: str) -> str:
        return eg.parse_signed(eg.signed_bytes(self.queue[rid]["message"], "s")).host

    def grants(self) -> list:
        return [c for c in self.calls if c[0] == "grant"]

    def filed_hosts(self) -> list[str]:
        return [eg.parse_signed(eg.signed_bytes(f.message, "s")).host for f in self.files]


def booted(hosts=("files.example.org",), **kw) -> World:
    w = World(hosts, **kw)
    w.settle(2)
    return w


# --- T1: the card's words are the template plus the logged host ------------------------------------------------------
def test_t1_the_card_text_is_the_template_plus_the_logged_host_and_nothing_an_agent_wrote():
    when = at(T0 + 480)
    a, b = C.push_body("evil.example", 3, when), C.push_body("other.example", 3, when)
    assert a.replace("evil.example", "other.example") == b and "evil.example" not in b
    assert C.title("evil.example", 3).replace("evil.example", "other.example") == C.title("other.example", 3)
    assert C.push_body("evil.example", 4, when) != a and C.push_body("evil.example", 3, at(T0)) != a
    # the module's word functions take only a host, a slot and a time: no other input exists to steer through
    import inspect
    assert list(inspect.signature(C.push_body).parameters) == ["host", "slot", "expires"]
    assert list(inspect.signature(C.title).parameters) == ["host", "slot"]
    # a punycode name is shown as received, in the same A-label form the signed message carries
    assert "xn--e1afmkfd.example" in C.push_body("xn--e1afmkfd.example", 1, when)


def test_t1_the_push_repeats_the_signed_words_and_claims_no_authorship():
    body = C.push_body("files.example.org", 3, at(T0))
    for key in eg.CHOICES:
        assert eg.option_text(key) in body
    assert "did not write" not in body and "attest" not in body.lower()
    assert "try again" in body                              # the refused request is never replayed: the agent retries


def test_t1_an_agent_cannot_steer_the_words_by_choosing_its_host_name():
    """The host is the one free field. A hostile name that is not a plain host name never reaches a card at all."""
    w = World(["files.example.org", "Evil.Example", "a b.example", "x.example\nINJECT", "é.example", "ab"])
    w.settle(2)
    assert set(w.flow.hosts) == {"files.example.org"}


# --- T2 / mutation target: the one open-card rule --------------------------------------------------------------------
def test_t2_a_card_is_filed_when_nothing_is_open_and_only_one_at_a_time():
    w = booted(("one.example.org", "two.example.org", "three.example.org"))
    assert len(w.files) == 1 and len(w.pushes) == 1
    for _ in range(100):                                     # 200 s of ticks with the card unanswered
        w.tick(2.0)
    assert len(w.files) == 1, "a second card was filed while one was open"


def test_t2_another_sessions_open_card_in_the_queue_blocks_filing_until_it_is_gone():
    w = World(["files.example.org"])
    w.open_elsewhere = True
    w.settle(2)
    assert w.files == [] and ("scan",) in w.calls
    for _ in range(5):
        w.tick(2.0)
    assert w.files == []
    w.open_elsewhere = False
    w.tick(C.SCAN_RETRY_S)
    assert len(w.files) == 1


def test_t2_the_scan_counts_a_card_only_while_it_is_open_unexpired_levains_and_unrefused():
    class Req:
        def __init__(self, ns=NS.EGRESS_HOST_NAMESPACE, who="levain", refused=None, exp=T0 + 100):
            self.namespace, self.requester, self.refused, self.expires_at = ns, who, refused, at(exp)

    rows = lambda req, state="open", mtime=T0 - 10: [("lv-1.json", mtime, req, state)]  # noqa: E731
    assert C.open_card_scan(rows(Req()), T0) is True
    assert C.open_card_scan(rows(Req(exp=T0 - 1)), T0) is False           # expired
    assert C.open_card_scan(rows(Req(), "signed"), T0) is False
    assert C.open_card_scan(rows(Req(), "denied"), T0) is False
    assert C.open_card_scan(rows(Req(ns="levain-review-clear")), T0) is False
    assert C.open_card_scan(rows(Req(who="other")), T0) is False
    assert C.open_card_scan(rows(Req(refused="no")), T0) is False         # never signable: asks nothing of him
    assert C.open_card_scan(rows(Req(), mtime=T0 - C.CARD_TTL_S - 1), T0) is False    # older than any open card can be
    assert C.open_card_scan(rows(None), T0) is False and C.open_card_scan([], T0) is False


def test_t2_a_deny_frees_the_slot_at_once_and_a_denied_host_is_never_carded_again():
    w = booted(("junk.example.org", "real.example.org"))
    first = w.filed_hosts()[0]
    w.deny()
    w.tick(2.0)
    assert w.filed_hosts() == [first, "real.example.org" if first == "junk.example.org" else "junk.example.org"]
    w.log += [first] * 20
    for _ in range(40):
        w.tick(C.POLL_S)
        w.deny() if w.queue[w.card()]["state"] == "open" else None
    assert w.filed_hosts().count(first) == 1


def test_t2_a_lapsed_host_is_carded_once_more_and_then_never_again():
    w = booted(("only.example.org",))
    assert len(w.files) == 1
    w.tick(C.CARD_TTL_S + 1)                   # nobody tapped
    w.settle(3)
    assert w.filed_hosts() == ["only.example.org"] * 2 and w.flow.hosts["only.example.org"].lapses == 1
    w.tick(C.CARD_TTL_S + 1)
    for _ in range(10):
        w.tick(C.POLL_S)
    assert len(w.files) == 2 and w.flow.hosts["only.example.org"].state == C.LAPSED
    assert any("lapsed twice" in n for n in w.flow.end_notes())


def test_t2_forty_junk_hosts_cannot_starve_the_one_the_agent_keeps_retrying():
    """C3: the agent chooses which hosts are refused. Junk is seen once; the legitimate host rises with its retries."""
    w = World([])
    w.open_elsewhere = True                                  # hold filing while the polls come in
    junk = [f"junk{i}.example.org" for i in range(40)]
    for _ in range(10):
        w.log += junk[:8] + ["real.example.org"]
        junk = junk[8:] + junk[:8]
    for _ in range(12):
        w.tick(C.POLL_S)
    assert w.flow.hosts["real.example.org"].polls >= 10
    w.open_elsewhere = False
    w.tick(C.SCAN_RETRY_S)
    assert w.filed_hosts() == ["real.example.org"]


# --- T3: live list dedupe, removal, deny -----------------------------------------------------------------------------
def granted(host="files.example.org", choice="s") -> World:
    w = booted((host,))
    w.tap(choice=choice)
    w.tick(2.0)
    assert w.flow.hosts[host].state == C.GRANTED, w.flow.end_notes()
    return w


def test_t3_a_host_root_already_lists_gets_no_card():
    w = World(["files.example.org"])
    w.live["files.example.org"] = ("eh-" + "1" * 24, at(T0 + 3600))
    w.settle(4)
    assert w.files == []


def test_t3_a_granted_host_that_vanishes_before_its_expiry_is_suppressed_not_recarded():
    w = granted()
    w.tick(C.LIST_S)                                         # a list that shows it (the prior set is now non-empty)
    assert "files.example.org" in w.flow.prior
    w.live.clear()                                           # revoked or repaired
    w.tick(C.LIST_S)
    h = w.flow.hosts["files.example.org"]
    assert h.state == C.REMOVED
    w.log += ["files.example.org"] * 3
    for _ in range(6):
        w.tick(C.POLL_S)
    assert len(w.files) == 1
    assert any("removed before its expiry (revoked or repaired)" in n for n in w.flow.end_notes())


def test_t3_a_host_whose_entry_expired_normally_may_be_carded_again():
    w = granted()
    w.tick(C.LIST_S)
    w.tick(9 * 3600)                                         # past the 8 h bound: the entry is gone by expiry
    w.log += ["files.example.org"]
    for _ in range(4):
        w.tick(C.POLL_S)
    assert len(w.files) == 2 and w.flow.hosts["files.example.org"].state == C.CARDED


def test_t3_an_error_list_or_another_store_never_counts_as_a_removal():
    w = granted()
    w.tick(C.LIST_S)
    w.live.clear()
    w.list_answer = "egress-grant-ledger"                    # the list failed: nothing can be inferred
    w.tick(C.LIST_S)
    assert w.flow.hosts["files.example.org"].state == C.GRANTED
    w.list_answer = None
    w.store = "store-other999"                               # a list of another store
    w.tick(C.LIST_S)
    assert w.flow.hosts["files.example.org"].state == C.GRANTED


def test_t3_an_empty_prior_list_is_not_a_removal():
    w = granted()                                            # no list has shown the entry yet
    w.live.clear()
    w.tick(C.LIST_S)
    assert w.flow.hosts["files.example.org"].state == C.GRANTED


def test_t3_a_denied_card_suppresses_the_host_for_the_session():
    w = booted()
    w.deny()
    w.tick(2.0)
    assert w.flow.hosts["files.example.org"].state == C.DENIED
    assert any("denied" in n for n in w.flow.end_notes())


# --- T4: the freshness bounds ----------------------------------------------------------------------------------------
def test_t4_the_card_is_signable_for_less_than_roots_freshness_window():
    w = booted()
    [f] = w.files
    assert f.ttl_s == C.CARD_TTL_S <= 480 < eg.FRESH_S and C.START_CUTOFF_S < eg.FRESH_S


def test_t4_a_tap_seen_inside_the_cutoff_is_delivered_and_one_seen_after_it_is_not():
    w = booted()
    issued = eg.parse_signed(eg.signed_bytes(w.files[0].message, "s")).issued.timestamp()
    w.tap()
    w.tick(2.0)
    [call] = w.grants()
    assert call[3] <= issued + C.START_CUTOFF_S and call[2] == pytest.approx(issued + C.START_CUTOFF_S)

    late = booted()
    late.tap()
    late.wall = issued + C.START_CUTOFF_S + 1                # wall 571 s after issue
    late.mono += C.START_CUTOFF_S + 1
    late.tick()
    assert late.grants() == [] and late.flow.hosts["files.example.org"].outcome == "late"
    assert any("too late" in n for n in late.flow.end_notes())


def test_t4_a_delivery_that_starts_at_569_runs():
    w = booted()
    issued = eg.parse_signed(eg.signed_bytes(w.files[0].message, "s")).issued.timestamp()
    w.tap()
    w.wall, w.mono = issued + 569, w.mono + 569
    w.tick()
    assert len(w.grants()) == 1


def test_t4_a_stalled_monotonic_clock_does_not_hide_a_wall_cutoff_and_says_the_clock_moved():
    """A sleep: the wall clock runs on, the monotonic one stalls. The wall cutoff wins; the note is not 'too late'."""
    w = booted()
    w.tap()
    w.wall += 800
    w.tick(mono_dt=3.0)
    assert w.grants() == []
    assert w.flow.hosts["files.example.org"].outcome == "clock"
    assert any("clock moved" in n for n in w.flow.end_notes())


def test_t4_a_last_second_tap_is_read_before_the_card_lapses():
    w = booted()
    w.tap()
    w.tick(C.CARD_TTL_S + 1.0)                               # the Signer took it at 479.9, the next read is at 481
    assert len(w.grants()) == 1


# --- T5 / T7: what is delivered is exactly what the phone signed ------------------------------------------------------
def test_t5_the_stdin_is_exactly_the_four_fields_and_the_message_is_the_cards_own():
    w = booted()
    w.tap(choice="d")
    w.tick(2.0)
    [(_, payload, _, _)] = w.grants()
    assert set(payload) == {"message", "sig", "signer_fp", "choice"}
    assert payload["message"] == w.files[0].message and payload["choice"] == "d" and payload["signer_fp"] == FP
    raw = C.stdin_bytes(payload)
    assert json.loads(raw) == payload and raw == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    from levain.slot.runtime_grammar import parse_stdin
    parsed = parse_stdin("egress-grant", raw)                  # root's own stdin grammar takes it
    assert (parsed.message, parsed.choice, parsed.signer_fp) == (payload["message"], "d", FP)


MSG = None


def _msg():
    global MSG
    if MSG is None:
        now = at(T0)
        MSG = eg.build_message(STORE, eg.new_grant_id(), SESSION, 3, "files.example.org", now, now + timedelta(hours=8), RP)
    return MSG


@pytest.mark.parametrize("tamper", [
    lambda r: r.update(signed_message=r["signed_message"] + "x"),
    lambda r: r.update(signed_message=r["signed_message"].replace("files.example.org", "evil.example.org")),
    lambda r: r.update(namespace="levain-confirm"),
    lambda r: r.update(namespace=None),
    lambda r: r.pop("choice"),
    lambda r: r.update(choice="x"),
    lambda r: r.update(choice="*"),
    lambda r: r.update(signer_fp="SHA256:short"),
    lambda r: r.update(signer_fp=5),
    lambda r: r.update(message_sha256="0" * 64),
    lambda r: r.pop("signed_message"),
])
def test_t5_a_record_that_is_not_over_the_cards_own_bytes_is_not_delivered(tamper):
    rec = json.loads(record_for(_msg(), "s"))
    tamper(rec)
    assert C.read_signed(json.dumps(rec).encode(), SIG.encode(), _msg()) is None


def test_t5_a_choice_signed_for_one_text_is_not_read_as_the_other_and_the_good_record_reads():
    rec = json.loads(record_for(_msg(), "s"))
    rec["choice"] = "d"                                      # the record says d, the signed text is s's
    assert C.read_signed(json.dumps(rec).encode(), SIG.encode(), _msg()) is None
    assert C.read_signed(record_for(_msg(), "s"), SIG.encode(), _msg())["choice"] == "s"
    assert C.read_signed(record_for(_msg(), "d"), SIG.encode(), _msg())["choice"] == "d"


@pytest.mark.parametrize("sig", [b"", b"\x00bad", "é".encode(), b"x" * 8001])
def test_t5_a_signature_root_could_not_read_is_not_delivered(sig):
    assert C.read_signed(record_for(_msg(), "s"), sig, _msg()) is None


def test_t5_unreadable_bytes_and_oversize_records_read_as_nothing():
    assert C.read_signed(b"not json", SIG.encode(), _msg()) is None
    assert C.read_signed(b"[]", SIG.encode(), _msg()) is None
    assert C.read_signed(b"\xff\xfe", SIG.encode(), _msg()) is None
    assert C.read_signed(b"x" * (16 * 1024 + 1), SIG.encode(), _msg()) is None
    assert C.read_signed(record_for(_msg(), "s"), SIG.encode(), "not the card's message") is None


def test_t7_s_and_d_are_each_delivered_with_their_own_choice_and_an_unusable_record_is_dead():
    for key in ("s", "d"):
        w = booted()
        w.tap(choice=key)
        w.tick(2.0)
        assert w.grants()[0][1]["choice"] == key
    w = booted()
    w.queue[w.card()].update(state="signed", payload=None)    # a signed answer read_signed refused
    w.tick(2.0)
    assert w.grants() == [] and w.flow.hosts["files.example.org"].outcome == "unreadable"


# --- T6: the passkey site --------------------------------------------------------------------------------------------
ORIGINS = [None, "", "https://cockpit.example.ts.net:8443", "https://localhost:8443", "https://127.0.0.1:8443",
           "https://a.b.c.d.example", "http://plain.example", "https://UPPER.example", "https://[::1]:8443",
           "https://" + "a" * 64 + ".example", "https://x.example:8443/path", "https://a.example.1"]


@pytest.mark.parametrize("origin", ORIGINS)
def test_t6_queue_rp_equals_the_dispatch_modules_over_a_table_of_origins(origin, tmp_path):
    from levain.consent import cockpit_origin
    from levain.consent.passkey import is_host_name
    from levain.dispatch import phone_config

    cockpit_origin.write_origin(tmp_path, origin)
    published = cockpit_origin.read_origin(tmp_path)
    try:
        want = phone_config.queue_rp(tmp_path)
    except ValueError:
        want = None
    try:
        got = C.queue_rp(published)
    except ValueError:
        got = None
    assert got == want
    assert C._passkey_host(want) == is_host_name(want)


@pytest.mark.parametrize("host", ["a.example", "1.2.3.4", "a.example.1", "A.example", "a..example", "-a.example", "", "x" * 254,
                                  "a" * 64 + ".example", "localhost", None, 5])
def test_t6_the_passkey_host_check_equals_passkeys_over_a_table(host):
    from levain.consent.passkey import is_host_name
    assert C._passkey_host(host) == is_host_name(host)


def test_t6_with_no_cockpit_no_card_is_filed_and_the_end_note_says_so():
    w = World(["files.example.org"], rp=None)
    w.settle(4)
    assert w.files == [] and ("scan",) not in w.calls
    assert any("cockpit was not running" in n and "files.example.org" in n for n in w.flow.end_notes())
    w.step(C.Cockpit(RP))                                    # the cockpit starts: the next tick may card it
    w.tick(C.SCAN_RETRY_S)
    assert len(w.files) == 1


# --- T8: root's names -------------------------------------------------------------------------------------------------
def _exported_names() -> set[str]:
    names = set()
    for mod in (EV, EL, EVY):
        names |= {v for k, v in vars(mod).items() if k.startswith("ERR_") and isinstance(v, str)}
    names |= {ES.NOT_READY, ES.DIR_UNTRUSTED} | set(eg.REASONS)
    names |= {"egress-grant-used", "egress-grant-revoked", "no-session", "session-busy"}   # literal in the verbs
    return names


def test_t8_every_name_root_can_answer_is_mapped_and_nothing_else_is():
    """Derived from the exported constants plus the literal names, so a new refusal name fails here until it is
    mapped. Reproduces X8: ``egress-grant-dir-untrusted`` was missing from the first map."""
    assert _exported_names() == set(C.NAMES)
    for name, (cls, words) in C.NAMES.items():
        assert cls in {C.RETRY, C.SPENT, C.ALREADY, C.OFF, C.SKEW, C.OPERATOR, C.UNSURE, C.POLL, C.FRESH, C.UNENROLLED}
        assert words and words == C.refusal_words(name) and cls == C.refusal_class(name)


def test_t8_an_unmapped_name_is_spent_and_never_shown_as_anything_but_a_plain_token():
    assert C.refusal_class("some-new-name") == C.SPENT and C.refusal_words("some-new-name") == "root answered some-new-name"
    assert "\x1b" not in C.refusal_words("x\x1b[31m") and "x" not in C.refusal_words("x\x1b[31m").split()[-1:]
    assert C.refusal_class(None) == C.SPENT


def test_t8_the_skew_names_say_root_is_older_and_name_the_repair():
    for name in ("choice", "writable-stale"):
        assert "root is older" in C.refusal_words(name) and "repair" in C.refusal_words(name)


RETRY_NAMES = ("egress-grant-not-active", "session-busy", "egress-grant-busy", "egress-grant-write-failed")


@pytest.mark.parametrize("name", RETRY_NAMES)
def test_t8_a_retryable_refusal_retries_at_10_then_20_seconds_and_stops_at_three_attempts(name):
    w = booted()
    w.tap()
    w.grant_answers = [name, name, name, name, name]
    w.tick(2.0)
    assert len(w.grants()) == 1
    w.tick(9.0)
    assert len(w.grants()) == 1                              # not before +10 s
    w.tick(1.0)
    assert len(w.grants()) == 2
    w.tick(19.0)
    assert len(w.grants()) == 2
    w.tick(1.0)
    assert len(w.grants()) == 3
    for _ in range(30):
        w.tick(10.0)
    assert len(w.grants()) == C.DELIVER_MAX == 3             # bounded: one card never costs more than 3 verifications
    assert w.flow.hosts["files.example.org"].state == C.DEAD
    assert any("no grant was made" in n for n in w.flow.end_notes())


def test_t8_a_retry_that_cannot_start_inside_the_cutoff_is_not_made():
    w = booted()
    issued = eg.parse_signed(eg.signed_bytes(w.files[0].message, "s")).issued.timestamp()
    w.tap()
    w.grant_answers = ["egress-grant-not-active", None]
    w.wall, w.mono = issued + 565, w.mono + 565              # the first start is at 565: +10 s would be 575 > 570
    w.tick()
    assert len(w.grants()) == 1
    for _ in range(10):
        w.tick(10.0)
    assert len(w.grants()) == 1 and w.flow.hosts["files.example.org"].state == C.DEAD


NO_RETRY = ["egress-grant-expired", "egress-grant-used", "egress-grant-revoked", "egress-grant-unenrolled",
            "egress-grant-sig", "egress-grant-ledger-full", "egress-grant-superseded", "egress-grant-host-routed",
            "fresh", "never-grant", "something-new"]


@pytest.mark.parametrize("name", NO_RETRY)
def test_t8_a_spent_refusal_is_not_retried_and_the_note_says_no_grant_was_made_or_why(name):
    w = booted()
    w.tap()
    w.grant_answers = [name, None, None]
    w.tick(2.0)
    for _ in range(10):
        w.tick(10.0)
    assert len(w.grants()) == 1 and w.flow.hosts["files.example.org"].state == C.DEAD
    assert not any("was allowed" in n for n in w.flow.end_notes())


def test_t8_a_timed_out_delivery_is_retried_and_a_used_answer_then_does_not_claim_no_grant():
    """Root may have published before the run was ended: ``egress-grant-used`` after a timeout is not 'spent'."""
    w = booted()
    w.tap()
    w.grant_answers = ["timeout", "egress-grant-used"]
    w.tick(2.0)
    w.tick(10.0)
    assert len(w.grants()) == 2
    notes = " ".join(w.flow.end_notes())
    assert "may be allowed" in notes and "no grant was made" not in notes


def test_t8_unenrolled_is_dead_with_the_enrol_flag_named_and_no_second_card_for_the_host():
    w = booted()
    w.tap()
    w.grant_answers = ["egress-grant-unenrolled"]
    w.tick(2.0)
    for _ in range(10):
        w.tick(C.POLL_S)
    assert len(w.files) == 1 and len(w.grants()) == 1
    assert any("--egress-host" in n and "enrol" in n for n in w.flow.end_notes())


@pytest.mark.parametrize("name", ["choice", "writable-stale"])
def test_t8_skew_turns_the_flow_off_after_one_wasted_tap_and_files_no_second_card(name):
    """X9: ``writable-stale`` used to re-file once; it now is the CA3 skew (root older than this levain)."""
    w = booted(("one.example.org", "two.example.org"))
    w.tap()
    w.grant_answers = [name]
    w.tick(2.0)
    n_calls = len(w.calls)
    for _ in range(20):
        w.tick(C.POLL_S)
    assert len(w.files) == 1 and w.flow.off is not None and len(w.calls) == n_calls
    assert any("root is older" in n for n in w.flow.end_notes())


# --- T9: granted means root said ok about this card -------------------------------------------------------------------
def test_t9_ok_for_this_card_is_granted_and_says_until_when():
    w = granted()
    notes = w.flow.end_notes()
    assert any("files.example.org was allowed until" in n and "root answered ok" in n for n in notes)


def test_t9_ok_for_another_grant_host_or_choice_is_not_reported_as_granted():
    for bad in ({"grant": "eh-" + "9" * 24}, {"host": "other.example.org"}, {"choice": "d"}, {"grant": None}):
        w = booted()
        w.tap(choice="s")
        w.flow, _ = w.flow.advance(C.Tick(), w.wall, w.mono)
        w.flow, cmds = w.flow.advance(C.Answer(w.card(), "signed", w.queue[w.card()]["payload"]), w.wall, w.mono)
        [deliver] = [c for c in cmds if isinstance(c, C.Deliver)]
        g = eg.parse_signed(eg.signed_bytes(deliver.payload["message"], "s"))
        result = {"grant": g.grant, "host": g.host, "expires": eg.stamp(g.expires), "choice": "s"}
        result.update(bad)
        w.flow, _ = w.flow.advance(C.Delivered(None, result), w.wall, w.mono)
        assert w.flow.hosts["files.example.org"].state == C.DEAD
        assert not any("was allowed" in n for n in w.flow.end_notes())


def test_t9_unsure_is_granted_with_the_power_loss_note():
    w = booted()
    w.tap()
    w.grant_answers = ["egress-grant-unsure"]
    w.tick(2.0)
    assert w.flow.hosts["files.example.org"].state == C.GRANTED
    assert any("may not survive a power loss" in n and "was allowed" in n for n in w.flow.end_notes())


def test_t9_a_tap_without_an_ok_is_never_reported_allowed():
    w = booted()
    w.tap()
    w.grant_answers = ["egress-grant-not-active"] * 3
    for _ in range(10):
        w.tick(10.0)
    assert not any("was allowed" in n for n in w.flow.end_notes())


# --- T11: the poll ---------------------------------------------------------------------------------------------------
def test_t11_the_cursor_root_returned_is_the_one_sent_next():
    w = World(["a.example.org", "b.example.org"])
    w.open_elsewhere = True
    w.settle(1)
    w.tick(C.POLL_S + 1)
    polls = [c for c in w.calls if c[0] == "poll"]
    assert polls[0][1] == 0 and polls[1][1] == 2


def test_t11_a_cursor_past_the_end_restarts_at_zero_and_the_log_error_backs_off():
    w = World(["a.example.org"])
    w.open_elsewhere = True
    w.settle(1)
    w.flow.cursor = 999                                      # a rotated or truncated log
    w.tick(C.POLL_S)
    cursors = [c[1] for c in w.calls if c[0] == "poll"]
    assert 999 in cursors and cursors[-1] == 0
    w2 = World(["a.example.org"])
    w2.open_elsewhere = True
    w2.poll_answer = EV.ERR_LOG
    w2.settle(2)
    n = len([c for c in w2.calls if c[0] == "poll"])
    w2.tick(C.POLL_SLOW_S - 1)
    assert len([c for c in w2.calls if c[0] == "poll"]) == n
    w2.tick(2.0)
    assert len([c for c in w2.calls if c[0] == "poll"]) == n + 1
    assert any(EV.ERR_LOG in n for n in w2.flow.end_notes())


def test_t11_an_empty_log_slows_the_poll_after_three_empty_answers_and_a_host_speeds_it_up():
    w = World([])
    w.open_elsewhere = True
    while len([c for c in w.calls if c[0] == "poll"]) < C.EMPTY_POLLS_SLOW:
        w.tick(C.POLL_S)
    before = len([c for c in w.calls if c[0] == "poll"])
    w.tick(C.POLL_S + 1)
    assert len([c for c in w.calls if c[0] == "poll"]) == before      # slowed to 30 s after three empties
    w.tick(C.POLL_SLOW_S)
    assert len([c for c in w.calls if c[0] == "poll"]) == before + 1
    w.log.append("late.example.org")
    w.tick(C.POLL_SLOW_S)
    assert "late.example.org" in w.flow.hosts
    n = len([c for c in w.calls if c[0] == "poll"])
    w.tick(C.POLL_S)
    assert len([c for c in w.calls if c[0] == "poll"]) == n + 1       # back to 10 s after a host


def test_t11_a_flood_of_2000_lines_from_40_hosts_is_all_seen_within_six_polls_and_a_full_poll_goes_again_at_once():
    hosts = [f"host{i:02d}.example.org" for i in range(40)]
    w = World([hosts[i % 40] for i in range(2000)])
    w.open_elsewhere = True
    w.poll_cap = 6
    w.tick(0.0)                                              # one tick: each full poll starts the next at once
    assert w.polls == 6 and set(hosts) <= set(w.flow.hosts)


# --- T12 / T13: setup and kind ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["egress-grant-kind", "egress-grant-class"])
@pytest.mark.parametrize("verb", [C.REFUSALS, C.GRANTS_LIST])
def test_t13_a_kind_or_class_refusal_on_any_call_turns_the_flow_off_with_one_note_and_no_more_calls(name, verb):
    """Reproduces an unattended session getting a card if the S2 kind filter ever drifted."""
    w = World(["files.example.org"])
    w.open_elsewhere = True
    if verb == C.REFUSALS:
        w.poll_answer = name
    else:
        w.list_answer = name
    w.settle(2)
    assert w.flow.off is not None
    n = len(w.calls)
    for _ in range(30):
        w.tick(C.POLL_S)
    assert len(w.calls) == n and w.files == []
    assert sum("not one host cards can allow" in x for x in w.flow.end_notes()) == 1


def test_t13_a_kind_refusal_on_the_delivery_also_ends_the_flow():
    w = booted()
    w.tap()
    w.grant_answers = ["egress-grant-kind"]
    w.tick(2.0)
    assert w.flow.off is not None
    n = len(w.calls)
    w.tick(60.0)
    assert len(w.calls) == n


@pytest.mark.parametrize("name,words", [("egress-grant-not-ready", "not set up"), ("egress-grant-dir-untrusted", "repair")])
def test_t12_not_ready_and_a_damaged_setup_turn_the_flow_off_and_say_why(name, words):
    w = World(["files.example.org"])
    w.list_answer = name
    w.settle(3)
    assert w.flow.off is not None and w.files == []
    assert any(words in n for n in w.flow.end_notes())
    n = len(w.calls)
    w.tick(120.0)
    assert len(w.calls) == n


# --- T18: store, deadline, never-granted hosts ------------------------------------------------------------------------
def test_t18_the_cards_store_is_the_one_root_listed_and_the_session_deadline_is_issue_plus_8_hours():
    w = World(["files.example.org"])
    w.store = "store-fromroot1"
    w.settle(3)
    g = eg.parse_signed(eg.signed_bytes(w.files[0].message, "s"))
    assert g.store == "store-fromroot1" and g.session == SESSION and g.slot == 3 and g.rp == RP
    assert g.expires == g.issued + timedelta(seconds=eg.SESSION_TTL_MAX_S)        # no invented session wall
    assert eg.parse_signed(eg.signed_bytes(w.files[0].message, "d")).expires == g.issued + timedelta(seconds=eg.DAY_TTL_S)


@pytest.mark.parametrize("host", ["localhost", "foo.internal", "metadata.example.org", "box.local"])
def test_t18_a_never_granted_host_files_nothing_and_is_noted_once(host):
    w = World([host, host, host])
    w.settle(4)
    w.log += [host] * 3
    for _ in range(4):
        w.tick(C.POLL_S)
    assert w.files == []
    assert sum(host in n and "never allowed" in n for n in w.flow.end_notes()) == 1


# --- T23: the module is pure ------------------------------------------------------------------------------------------
def test_t23_egress_cards_imports_no_subprocess_or_os_and_opens_nothing():
    tree = ast.parse((ROOT / "levain/slot/egress_cards.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
            mods |= {a.name for a in node.names if (node.module or "") in ("", ".")}
    assert not mods & {"subprocess", "os", "socket", "pathlib", "shutil", "tempfile", "threading", "select"}, mods
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"open", "exec", "eval", "__import__", "input"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"popen", "system", "spawn", "fork", "unlink", "write_text", "read_text", "write_bytes"}


def test_t23_the_reducer_never_mutates_the_flow_it_was_given():
    w = booted()
    before = repr(w.flow)
    w.flow.advance(C.Polled(("another.example.org",), 5), w.wall, w.mono)
    w.flow.advance(C.Tick(), w.wall + 1000, w.mono + 1000)
    assert repr(w.flow) == before


def test_t23_every_import_of_the_module_is_stdlib_or_the_two_format_modules():
    tree = ast.parse((ROOT / "levain/slot/egress_cards.py").read_text(encoding="utf-8"))
    levain = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("levain")}
    assert levain == {"levain.contracts.namespaces", "levain.slot"}, levain


# --- T25: a long host name -------------------------------------------------------------------------------------------
def long_host() -> str:
    labels = ["a" * 61, "b" * 61, "c" * 61, "d" * 63]
    host = ".".join(labels) + ".org"
    host = host[:253]
    assert len(host) == 253 and host[-1] != "." and not host.endswith("-")
    return host


def test_t25_a_253_character_host_shows_its_last_48_characters_and_the_title_fits():
    host = long_host()
    t = C.title(host, 3)
    assert host[-C.TITLE_HOST_MAX:] in t and host[:50] not in t and t.startswith("Allow …")
    assert len(t) < 200 and len(t) < 300
    first = C.push_body(host, 3, at(T0)).split("\n")[0]
    assert host[-48:] in first and host not in first
    assert C.shown_host("short.example.org") == "short.example.org"
    assert C.shown_host("x" * 48) == "x" * 48 and C.shown_host("x" * 49) == "…" + "x" * 48


def test_t25_the_full_host_still_rides_in_the_signed_message_and_the_card_files():
    host = long_host()
    w = World([host])
    w.settle(3)
    assert len(w.files) == 1 and host in w.files[0].message and w.filed_hosts() == [host]
    from levain.consent import sign
    assert len(w.files[0].title) <= sign._TEXT_MAX


# --- the end-notes -------------------------------------------------------------------------------------------------
def test_the_end_notes_name_waiting_and_suppressed_hosts_up_to_a_cap_then_count():
    w = World([f"h{i:02d}.example.org" for i in range(60)])
    w.open_elsewhere = True
    w.poll_cap = 10
    w.tick(0.0)
    notes = w.flow.end_notes()
    [line] = [n for n in notes if "waited without a card" in n]
    assert line.count(".example.org") == C.NOTES_MAX and "and 28 more" in line


def test_the_end_notes_say_how_the_card_reached_him():
    w = booted(phone_live=True)
    assert "host cards were pushed to your phone" in w.flow.end_notes()
    w = booted(phone_live=False)
    assert any("no phone was subscribed" in n and "notification center" in n for n in w.flow.end_notes())
    assert w.pushes and w.pushes[0].phone_live is False and w.pushes[0].rid == w.card()


def test_a_card_left_open_at_the_end_says_tapping_it_now_does_nothing():
    w = booted()
    assert any("left open" in n and "does nothing" in n for n in w.flow.end_notes())


def test_the_push_is_one_per_card_and_never_for_a_retry():
    w = booted()
    w.tap()
    w.grant_answers = ["egress-grant-not-active"] * 3
    for _ in range(10):
        w.tick(10.0)
    assert len(w.pushes) == 1
