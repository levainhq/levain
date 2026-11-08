"""3c-10 as one command, proven against a FAKE guest and a FAKE provider: no VM, no ssh, no network, no real key. The doctor
rows the verdicts expect are produced by the product's own ``levain.doctor`` code, so a wording change there breaks this test,
not the Monday sitting."""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from levain.broker import creds
from levain.doctor import CheckResult, _emit, _limit_verdict
from scripts.vm_harness import runbook as RB
from scripts.vm_harness import threec10 as T
from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, LINUX

LIMITED = "sk-or-v1-LIMITEDSECRET0123456789abcdef"
UNLIMITED = "sk-or-v1-UNLIMITEDSECRET0123456789abcdef"
CRED = "or-key"
STORE = DARWIN.store


class World:
    """One fake guest + one fake provider. ``usage`` is the provider's books (microdollars); a call adds ``charge`` to the
    lineage and, after ``lag`` reads of the key status, to ``usage``."""

    def __init__(self, charge=250_000, lineage_charge=None, lag=0, overrun=True, doctor_exc=None, preexisting=None):
        self.files = dict(preexisting or {})  # cred name -> credential json text
        self.argvs = []                     # every argv any call saw (a secret must never be in one)
        self.stdins = []
        self.rows = []
        self.usage = 1_000_000
        self.pending = 0
        self.lag = lag
        self.charge = charge
        self.lineage_charge = charge if lineage_charge is None else lineage_charge
        self.overrun = overrun
        self.doctor_exc = doctor_exc
        self.doctor_calls = 0
        self.fetches = []
        self.new_removed = 0
        self.foreign_new = None             # a .json.new another writer left on the guest (None: none)

    # guest(argv, timeout): capture's wrapper first
    def guest(self, argv, timeout=60):
        assert list(argv[:4]) == list(RB.CAPTURE_ARGV)
        inner = list(argv[4:])
        self.argvs.append(inner)
        out, rc = self.dispatch(inner)
        return "\n".join(out) + ("\n" if out else "") + f"{RB.RC_MARK} {rc}\n"

    def dispatch(self, inner):
        if inner[:3] == ["sudo", "-n", "/bin/cat"]:
            assert inner[3] == f"{STORE}/lineage/lineage.jsonl"
            return [json.dumps(r) for r in self.rows], 0
        if "doctor" in inner:
            self.doctor_calls += 1
            if self.doctor_exc:
                raise self.doctor_exc
            return self.doctor_rows(), 1          # doctor exits 1 on unrelated rows: the verdict ignores rc
        if inner == ["fire", "call"]:
            self.rows.append({"cost_microdollars": self.lineage_charge, "model_answered": "z-ai/glm-5.2", "run_id": "r-1",
                              "settled": "settled", "overrun": None})
            self.pending += self.charge
            return ["fired"], 0
        if inner == ["fire", "overrun"]:
            if self.overrun:
                self.rows.append({"cost_microdollars": 9, "model_answered": "m", "run_id": "r-2", "settled": "overrun",
                                  "overrun": 4242})
            return ["stopped"], 3
        raise AssertionError(inner)

    def doctor_rows(self):
        """The product's own row text for the credential currently installed."""
        import contextlib
        import io
        key = json.loads(self.files[CRED])["secret"]
        name = f"invoice ceiling ({CRED})"
        if key == LIMITED:
            r = _limit_verdict(name, 5.0, 4.75, None, True, " (as keys read it less than a minute ago)")
        else:
            r = _limit_verdict(name, None, None, None, None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit(CheckResult("slots", True, "fine"))
            _emit(r)
        return buf.getvalue().splitlines()

    def stdin_run(self, argv, text, timeout=60):
        self.argvs.append(list(argv))
        self.stdins.append(text)
        assert list(argv[:4]) == ["sudo", "-n", "/bin/sh", "-c"] and argv[5] == "sh", argv
        script, store, name = argv[4], argv[6], argv[7]
        assert store == STORE
        if script == T.INSTALL_SCRIPT:
            if argv[8] != "replace" and name in self.files:
                return f"cred-refused {name}: already exists\n"
            self.files[name] = text
            return f"cred-installed {name}\n"
        if script == T.REMOVE_SCRIPT:
            self.files.pop(name, None)
            return f"cred-removed {name}\n"
        if script == T.REMOVE_NEW_SCRIPT:
            if self.foreign_new is not None and text != self.foreign_new:
                return f"cred-foreign-new {name}\n"
            self.new_removed += 1
            return f"cred-removed-new {name}\n"
        if script == T.VERIFY_SCRIPT:
            return (f"cred-present {name}.json\n" if argv[8] == "all" and name in self.files else f"cred-absent {name}\n")
        raise AssertionError(script)

    def fetch(self, url, headers, timeout):
        self.fetches.append((url, dict(headers)))
        assert url == T.KEY_URL and headers["Authorization"] == "Bearer " + LIMITED
        if self.pending and self.lag <= 0:
            self.usage += self.pending
            self.pending = 0
        elif self.pending:
            self.lag -= 1
        return 200, json.dumps({"data": {"usage": self.usage / 1_000_000, "limit": 5}}).encode()


def cfg(**kw):
    kw.setdefault("keys", {"limited": LIMITED, "unlimited": UNLIMITED})
    kw.setdefault("call", ("fire", "call"))
    kw.setdefault("overrun_call", ("fire", "overrun"))
    kw.setdefault("settle_poll_s", 0.0)
    return T.Config(DARWIN, "/tmp/install", CRED, **kw)


def go(tmp_path, w=None, **kw):
    w = w or World()
    ev = tmp_path / "ev"
    ev.mkdir()
    ticks = iter(range(10_000))
    out = T.run_3c10(w.guest, w.stdin_run, ev, cfg(**kw), fetch=w.fetch, sleep=lambda s: None,
                     clock=lambda: float(next(ticks)))
    return w, ev, out


def by_id(steps):
    return {s.id: s for s in steps}


# ---- the verdicts against the product's real rows --------------------------------------------------------------------

def test_the_real_doctor_rows_match_the_expected_verdicts():
    w = World()
    w.files[CRED] = T.credential_json(CRED, LIMITED)
    lim = w.doctor_rows()
    w.files[CRED] = T.credential_json(CRED, UNLIMITED)
    none = w.doctor_rows()
    v = {sid: (re.compile(pat.replace("<cred>", re.escape(CRED))), sel.replace("<cred>", re.escape(CRED)))
         for sid, _n, _rc, pat, sel in T.EXPECTED if sid in ("T2", "T6")}
    for sid, lines in (("T2", lim), ("T6", none)):
        raw = RB.pick_line(lines, v[sid][1])
        assert raw and v[sid][0].fullmatch(raw), (sid, raw)
    assert not v["T2"][0].fullmatch(RB.pick_line(none, v["T2"][1]))       # the two verdicts do not overlap
    assert not v["T6"][0].fullmatch(RB.pick_line(lim, v["T6"][1]))


def test_expected_verdicts_are_single_patterns():
    for sid, _n, rc, pat, _sel in T.EXPECTED:
        RB.Verdict(rc, pat).check()
        assert "|" not in pat, sid


def test_credential_json_is_what_the_products_parser_reads_and_the_format_is_pinned():
    assert T.CRED_FORMAT == creds.FORMAT and T.CRED_NAME_RE.pattern == creds.NAME_RE.pattern
    raw = T.credential_json(CRED, LIMITED)
    c = creds.parse(raw.encode(), CRED)
    assert c.hosts == frozenset({"openrouter.ai"}) and c.kind == "static"
    assert T.credential_json(CRED, LIMITED) != raw                         # a fresh rev each time
    with pytest.raises(RB.RunbookError):
        T.credential_json("Bad Name", LIMITED)


def test_keys_user_is_the_layouts():
    import inspect

    from levain.slot import layout
    assert T.KEYS_USER == "_levain_keys"
    assert f'Row(L / "keys", "{T.KEYS_USER}", "{T.KEYS_USER}", 0o700)' in inspect.getsource(layout)


# ---- the run ---------------------------------------------------------------------------------------------------------

def test_full_run_passes_and_every_step_has_its_one_verdict(tmp_path):
    w, ev, (status, steps, path) = go(tmp_path)
    assert status == "PASS", path.read_text()
    assert [s.id for s in steps] == ["T1", "T2", "T3", "T4", "T5", "T6"] and all(s.status == "PASS" for s in steps)
    t3 = by_id(steps)["T3"].raw
    assert t3.startswith("AGREE lineage_microdollars=250000 provider_delta_microdollars=250000 rows=1")
    assert by_id(steps)["T4"].raw.startswith("overrun-row run_id=r-2 overrun=4242")
    assert w.files == {}                                                   # the credential is gone


def test_the_secret_never_appears_in_an_argv_or_in_evidence(tmp_path):
    w, ev, _ = go(tmp_path)
    flat = json.dumps(w.argvs)
    assert LIMITED not in flat and UNLIMITED not in flat
    assert any(LIMITED in t for t in w.stdins) and any(UNLIMITED in t for t in w.stdins)   # it went by stdin
    for p in ev.iterdir():
        text = p.read_text()
        assert LIMITED not in text and UNLIMITED not in text, p.name


def test_scrub_masks_a_key_that_a_raw_line_echoes(tmp_path):
    w = World()
    orig = w.doctor_rows
    w.doctor_rows = lambda: orig() + [f"  [WARN] invoice ceiling ({CRED}): echoed {LIMITED}"]
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert LIMITED not in (ev / "steps.log").read_text() and LIMITED not in path.read_text()


def test_expected_txt_is_written_before_any_guest_call(tmp_path):
    w = World()
    seen = []
    orig = w.guest
    w.guest = lambda argv, timeout=60: (seen.append((tmp_path / "ev" / "EXPECTED.txt").exists()), orig(argv, timeout))[1]
    go(tmp_path, w)
    # stdin_run (the install) and the first capture both come after EXPECTED.txt
    assert seen and all(seen)
    body = (tmp_path / "ev" / "EXPECTED.txt").read_text()
    assert "T2 | doctor, key with a limit | any |" in body and "tolerance" in body


def test_provider_settling_late_is_waited_for(tmp_path):
    w, ev, (status, steps, path) = go(tmp_path, World(lag=3))
    assert by_id(steps)["T3"].status == "PASS" and by_id(steps)["T3"].raw.count("provider_delta_microdollars=250000") == 1


def test_a_provider_that_never_moves_is_nomove_not_an_agreement(tmp_path):
    w = World(lag=10_000)
    w2, ev, (status, steps, path) = go(tmp_path, w, settle_wait_s=3.0)
    t3 = by_id(steps)["T3"]
    assert t3.status == "FAIL" and t3.raw.startswith("NOMOVE") and "provider_delta_microdollars=0" in t3.raw
    assert status == "FAIL"


def test_lineage_off_from_the_bill_beyond_tolerance_fails_and_within_it_passes(tmp_path):
    _w, _ev, (status, steps, _p) = go(tmp_path, World(charge=250_000, lineage_charge=300_000))
    assert by_id(steps)["T3"].status == "FAIL" and by_id(steps)["T3"].raw.startswith("DISAGREE")
    (tmp_path / "b").mkdir()
    _w, _ev, (status, steps, _p) = go(tmp_path / "b", World(charge=250_000, lineage_charge=255_000))
    assert by_id(steps)["T3"].status == "PASS"                              # 2% < 5%


def test_a_call_that_settled_nothing_is_not_an_agreement(tmp_path):
    w = World()
    orig = w.dispatch
    w.dispatch = lambda inner: ((["fired"], 0) if inner == ["fire", "call"] else orig(inner))   # no row, no charge
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    assert by_id(steps)["T3"].raw.startswith("NOCALL") and by_id(steps)["T3"].status == "FAIL"


def test_a_missing_overrun_row_fails_t4(tmp_path):
    _w, _ev, (status, steps, _p) = go(tmp_path, World(overrun=False))
    t4 = by_id(steps)["T4"]
    assert t4.status == "FAIL" and t4.raw.startswith("no-overrun-row")


def test_missing_inputs_are_unbound_and_the_run_is_incomplete(tmp_path):
    w, ev, (status, steps, path) = go(tmp_path, call=(), overrun_call=())
    s = by_id(steps)
    assert (s["T3"].status, s["T4"].status) == ("UNBOUND", "UNBOUND") and status == "INCOMPLETE"
    assert "RESULT: INCOMPLETE" in path.read_text()
    (tmp_path / "k").mkdir()
    w, ev, (status, steps, path) = go(tmp_path / "k", keys={"limited": LIMITED, "unlimited": None})
    s = by_id(steps)
    assert (s["T5"].status, s["T6"].status) == ("UNBOUND", "UNBOUND") and status == "INCOMPLETE"
    (tmp_path / "n").mkdir()
    w, ev, (status, steps, path) = go(tmp_path / "n", keys={"limited": None, "unlimited": UNLIMITED})
    s = by_id(steps)
    assert [s[i].status for i in ("T1", "T2", "T3", "T4")] == ["UNBOUND"] * 4 and s["T6"].status == "PASS"


def test_the_credential_is_removed_even_when_a_step_blows_up(tmp_path):
    w = World(doctor_exc=GuestError(255, "ssh dropped"))
    _w, ev, (status, steps, path) = go(tmp_path, w)
    s = by_id(steps)
    assert s["T2"].status == "FAIL" and "transport" in s["T2"].detail and status == "FAIL"
    assert w.files == {} and "removed" in (ev / "steps.log").read_text()


def test_a_failed_removal_is_said_loudly(tmp_path):
    w = World()
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        if argv[4] == T.REMOVE_SCRIPT:
            raise GuestError(1, "rm refused")
        return orig(argv, text, timeout)

    w.stdin_run = run
    ev = tmp_path / "ev"
    ev.mkdir()
    ticks = iter(range(10_000))
    status, _s, path = T.run_3c10(w.guest, w.stdin_run, ev, cfg(), fetch=w.fetch, sleep=lambda s: None,
                                  clock=lambda: float(next(ticks)))
    assert "NOT confirmed removed" in (ev / "steps.log").read_text()
    assert status == "INCOMPLETE" and "CLEANUP:" in path.read_text() and "RESULT: PASS" not in path.read_text()
    assert T.main.__code__ and {"PASS": 0}.get(status, 3) != 0


def test_nothing_is_installed_so_nothing_is_removed_when_no_key_is_given(tmp_path):
    w = World()
    go(tmp_path, w, keys={})
    assert w.stdins == [] and w.argvs == []


def test_linux_profile_uses_its_own_store_and_python(tmp_path):
    c = T.Config(LINUX, "/tmp/install", CRED)
    assert T._doctor_argv(c)[4] == LINUX.python
    assert T.expected_text(c).splitlines()[0].endswith("os=linux, cred=or-key")


# ---- the provider seam ---------------------------------------------------------------------------------------------

def test_usage_microdollars_reads_data_usage_and_refuses_the_rest():
    ok = lambda u, h, t: (200, b'{"data":{"usage":0.2503}}')                # noqa: E731
    assert T.usage_microdollars(ok, "k") == 250_300
    with pytest.raises(RB.RunbookError, match="401"):
        T.usage_microdollars(lambda u, h, t: (401, b"{}"), "k")
    for body in (b"not json", b"{}", b'{"data":{"usage":"x"}}', b'{"data":{"usage":-1}}', b'{"data":{"usage":true}}'):
        with pytest.raises(RB.RunbookError):
            T.usage_microdollars(lambda u, h, t, b=body: (200, b), "k")


def test_default_fetch_only_ever_talks_to_openrouter():
    with pytest.raises(RB.RunbookError, match="only openrouter.ai"):
        T.default_fetch("https://example.com/api/v1/key", {}, 1)
    with pytest.raises(RB.RunbookError):
        T.default_fetch("http://openrouter.ai/api/v1/key", {}, 1)


def test_a_bad_credential_name_refuses_before_any_evidence(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(RB.RunbookError):
        T.run_3c10(lambda *a, **k: "", lambda *a: "", ev, T.Config(DARWIN, "/i", "Bad Name"))
    assert list(ev.iterdir()) == []


# ---- L1 fixes ---------------------------------------------------------------------------------------------------------

def test_a_small_lineage_total_against_an_unmoved_provider_is_not_an_agreement(tmp_path):
    # lineage 500 < the 1000 microdollar floor, provider books unmoved: the old tolerance test said AGREE
    _w, _ev, (status, steps, _p) = go(tmp_path, World(charge=0, lineage_charge=500), settle_wait_s=3.0)
    t3 = by_id(steps)["T3"]
    assert t3.raw.startswith("NOMOVE") and t3.status == "FAIL" and status == "FAIL"


def test_a_transport_failure_after_the_write_leaves_the_file_and_says_so(tmp_path):
    w = World()
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        out = orig(argv, text, timeout)
        if argv[4] == T.INSTALL_SCRIPT:
            raise GuestError(255, "ssh dropped after the write")
        return out

    w.stdin_run = run
    _w, ev, (status, steps, _p) = go(tmp_path, w)
    s = by_id(steps)
    assert s["T1"].status == "ERROR" and status == "INCOMPLETE" and "UNCERTAIN" in s["T1"].detail
    assert CRED in w.files and w.new_removed == 1                         # <cred>.json is NEVER removed on an uncertain install
    assert [s[i].status for i in ("T2", "T3", "T4", "T5", "T6")] == ["UNBOUND"] * 5
    assert all(a[4] != T.REMOVE_SCRIPT for a in w.argvs if len(a) > 4)
    assert "RESULT: INCOMPLETE" in _p.read_text() and "check it by hand" in _p.read_text()
    assert ".json.new" in T.REMOVE_SCRIPT


def _sh(tmp_path, script, name, mode, stdin):
    """Run the REAL script under /bin/sh against a temp store; chown is a no-op stub (the sitting runs it as root)."""
    store = tmp_path / "store"
    (store / "keys" / "cred").mkdir(parents=True, exist_ok=True)
    fb = tmp_path / "fakebin"
    fb.mkdir(exist_ok=True)
    (fb / "chown").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(fb / "chown", 0o755)
    args = ["/bin/sh", "-c", script, "sh", str(store), name] + ([mode] if mode else [])
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, env=dict(os.environ, PATH=f"{fb}:{os.environ['PATH']}"))
    return r, store / "keys" / "cred"


def test_install_script_installs_a_new_credential(tmp_path):
    r, d = _sh(tmp_path, T.INSTALL_SCRIPT, "k", "new", '{"a":1}')
    assert r.returncode == 0 and r.stdout.strip() == "cred-installed k" and (d / "k.json").read_text() == '{"a":1}'
    assert not (d / "k.json.new").exists()


def test_install_script_refuses_an_existing_credential_and_leaves_it(tmp_path):
    (tmp_path / "store" / "keys" / "cred").mkdir(parents=True)
    (tmp_path / "store" / "keys" / "cred" / "k.json").write_text("HIS")
    r, d = _sh(tmp_path, T.INSTALL_SCRIPT, "k", "new", '{"a":1}')
    assert r.returncode == 0 and r.stdout.startswith("cred-refused k") and (d / "k.json").read_text() == "HIS"
    assert not (d / "k.json.new").exists()
    r, d = _sh(tmp_path, T.INSTALL_SCRIPT, "k", "replace", '{"a":2}')       # the sitting's own file may be replaced
    assert r.stdout.strip() == "cred-installed k" and (d / "k.json").read_text() == '{"a":2}'


def test_install_script_refuses_an_empty_credential(tmp_path):
    r, d = _sh(tmp_path, T.INSTALL_SCRIPT, "k", "new", "")
    assert r.stdout.startswith("cred-refused k: empty") and list(d.iterdir()) == []


def test_remove_script_removes_the_half_written_file_too(tmp_path):
    (tmp_path / "store" / "keys" / "cred").mkdir(parents=True)
    d = tmp_path / "store" / "keys" / "cred"
    (d / "k.json").write_text("x")
    (d / "k.json.new").write_text("half")
    r, d = _sh(tmp_path, T.REMOVE_SCRIPT, "k", None, "")
    assert r.returncode == 0 and list(d.iterdir()) == []


def test_a_refused_install_ends_refused_and_never_removes_his_file(tmp_path):
    w = World(preexisting={CRED: "HIS"})
    _w, ev, (status, steps, path) = go(tmp_path, w)
    s = by_id(steps)
    assert status == "REFUSED" and s["T1"].status == "REFUSED" and w.files == {CRED: "HIS"}
    assert [s[i].status for i in ("T2", "T3", "T4", "T5", "T6")] == ["UNBOUND"] * 5
    assert "RESULT: REFUSED" in path.read_text() and "cred-refused" in path.read_text()


def test_an_overall_word_for_a_timeout_is_incomplete_and_names_it(tmp_path):
    w = World(doctor_exc=subprocess.TimeoutExpired(["ssh"], 180))
    _w, ev, (status, steps, path) = go(tmp_path, w)
    t2 = by_id(steps)["T2"]
    assert t2.status == "ERROR" and "TimeoutExpired" in t2.detail and status == "INCOMPLETE"
    assert "RESULT: INCOMPLETE" in path.read_text() and w.files == {}


# ---- L3 round 1 fixes -------------------------------------------------------------------------------------------------

def test_a_removal_that_returns_ok_but_leaves_the_file_is_not_a_pass(tmp_path):
    w = World()
    orig = w.stdin_run
    w.stdin_run = lambda argv, text, timeout=60: ("cred-removed or-key\n" if argv[4] == T.REMOVE_SCRIPT else orig(argv, text, timeout))
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert status == "INCOMPLETE" and CRED in w.files and "STILL on the guest" in path.read_text()


def test_a_removal_timeout_is_not_a_pass(tmp_path):
    w = World()
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        if argv[4] == T.REMOVE_SCRIPT:
            raise subprocess.TimeoutExpired(["ssh"], 60)
        return orig(argv, text, timeout)

    w.stdin_run = run
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert status == "INCOMPLETE" and "TimeoutExpired" in path.read_text()


def test_a_clean_run_says_cleanup_ok(tmp_path):
    _w, _ev, (status, _s, path) = go(tmp_path)
    assert status == "PASS" and "CLEANUP: ok" in path.read_text()


def test_an_ambiguous_t1_never_lets_t5_replace_or_delete_his_file(tmp_path):
    """His or-key.json exists; T1's reply is lost (the script in fact refused). T5 must not run in replace mode."""
    w = World(preexisting={CRED: "HIS"})
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        out = orig(argv, text, timeout)
        if argv[4] == T.INSTALL_SCRIPT:
            raise GuestError(255, "ssh dropped before the answer")
        return out

    w.stdin_run = run
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert w.files == {CRED: "HIS"}                                         # never overwritten, never deleted
    assert not any(a[4] == T.REMOVE_SCRIPT for a in w.argvs if len(a) > 4)
    assert [a[8] for a in w.argvs if len(a) > 8 and a[4] == T.INSTALL_SCRIPT] == ["new"]   # T5 never ran
    assert status == "INCOMPLETE" and by_id(steps)["T5"].status == "UNBOUND"


def test_t5_replaces_only_after_a_confirmed_t1(tmp_path):
    w, _ev, (status, _s, _p) = go(tmp_path)
    modes = [a[8] for a in w.argvs if len(a) > 8 and a[4] == T.INSTALL_SCRIPT]
    assert modes == ["new", "replace"] and status == "PASS"


def test_an_install_that_prints_nothing_is_uncertain_not_ours(tmp_path):
    w = World(preexisting={CRED: "HIS"})
    w.stdin_run = lambda argv, text, timeout=60: "" if argv[4] == T.INSTALL_SCRIPT else World.stdin_run(w, argv, text, timeout)
    _w, _ev, (status, steps, _p) = go(tmp_path, w)
    assert w.files == {CRED: "HIS"} and status == "INCOMPLETE" and by_id(steps)["T1"].status == "ERROR"


def test_the_verify_script_reports_what_is_on_disk(tmp_path):
    r, d = _sh(tmp_path, T.VERIFY_SCRIPT, "k", "all", "")
    assert r.stdout.strip() == "cred-absent k"
    (d / "k.json").write_text("x")
    r, d = _sh(tmp_path, T.VERIFY_SCRIPT, "k", "all", "")
    assert r.stdout.startswith("cred-present k.json")
    r, d = _sh(tmp_path, T.VERIFY_SCRIPT, "k", "new", "")                  # "new" ignores <cred>.json
    assert r.stdout.strip() == "cred-absent k"
    (d / "k.json.new").write_text("x")
    r, d = _sh(tmp_path, T.VERIFY_SCRIPT, "k", "new", "")
    assert r.stdout.startswith("cred-present k.json.new")
    r, d = _sh(tmp_path, T.REMOVE_NEW_SCRIPT, "k", None, "x")
    assert (d / "k.json").exists() and not (d / "k.json.new").exists()      # removes the twin only


def test_the_stdin_runner_follows_the_selected_transport_not_the_profile(tmp_path, monkeypatch):
    from scripts.vm_harness import vm_run
    vm = "vend-test"
    monkeypatch.setattr(vm_run, "agent_answers", lambda *a, **k: False)
    guest, _spawn = vm_run.make_transport(vm, LINUX, tmp_path)
    assert guest.kind == "ssh" and LINUX.transport == "tart_exec" and LINUX.ssh_fallback
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="10.0.0.5\n" if "ip" in cmd else "out", stderr="")

    monkeypatch.setattr(RB.subprocess, "run", fake_run)
    run = RB.make_stdin_runner(vm, LINUX, tmp_path, guest.kind)
    assert run(["/bin/true"], "stdin", 5) == "out" and run(["/bin/true"], "stdin", 5) == "out"
    assert calls[-1][0] == vm_run.SSH and "exec" not in calls[-1]
    assert sum(1 for c in calls if "ip" in c) == 1                          # the address is resolved once
    # and with the agent answering, the same profile gets tart exec
    monkeypatch.setattr(vm_run, "agent_answers", lambda *a, **k: True)
    g2, _ = vm_run.make_transport(vm, LINUX, tmp_path)
    assert g2.kind == "tart_exec"
    RB.make_stdin_runner(vm, LINUX, tmp_path, g2.kind)(["/bin/true"], "x", 5)
    assert calls[-1][1] == "exec" and "-i" in calls[-1]
    with pytest.raises(RB.RunbookError):
        RB.make_stdin_runner(vm, LINUX, tmp_path, "carrier-pigeon")


def test_an_empty_tart_ip_is_a_guest_error_not_an_index_error(tmp_path, monkeypatch):
    monkeypatch.setattr(RB.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr=""))
    run = RB.make_stdin_runner("vend-test", DARWIN, tmp_path, "ssh")
    with pytest.raises(GuestError):
        run(["x"], "", 5)


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0])
def test_a_non_finite_or_negative_tolerance_refuses_before_any_evidence(tmp_path, bad):
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(RB.RunbookError, match="tolerance"):
        T.run_3c10(lambda *a, **k: "", lambda *a: "", ev, cfg(tolerance_pct=bad))
    assert list(ev.iterdir()) == []


def test_a_non_finite_provider_usage_is_an_error_not_a_crash(tmp_path):
    with pytest.raises(T.FetchError):
        T.usage_microdollars(lambda u, h, t: (200, b'{"data":{"usage":NaN}}'), "k")
    with pytest.raises(T.FetchError):
        T.usage_microdollars(lambda u, h, t: (200, b'{"data":{"usage":Infinity}}'), "k")
    w = World()
    w.fetch = lambda url, headers, timeout: (200, b'{"data":{"usage":NaN}}')
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert by_id(steps)["T3"].status == "ERROR" and status == "INCOMPLETE" and path.exists() and w.files == {}


def test_a_provider_timeout_is_error_not_fail(tmp_path):
    w = World()

    def boom(url, headers, timeout):
        raise T.FetchError("the provider could not be reached: TimeoutError")

    w.fetch = boom
    _w, ev, (status, steps, path) = go(tmp_path, w)
    assert by_id(steps)["T3"].status == "ERROR" and status == "INCOMPLETE"


def test_a_boolean_cost_or_overrun_is_not_a_number(tmp_path):
    w = World()
    orig = w.dispatch

    def d(inner):
        if inner == ["fire", "overrun"]:
            w.rows.append({"overrun": False, "run_id": "r-x"})
            return ["stopped"], 3
        return orig(inner)

    w.dispatch = d
    _w, ev, (status, steps, _p) = go(tmp_path, w)
    assert by_id(steps)["T4"].status == "FAIL" and by_id(steps)["T4"].raw.startswith("no-overrun-row")
    w2 = World()
    w2.lineage_charge = True
    (tmp_path / "b").mkdir()
    _w, _ev, (status, steps, _p) = go(tmp_path / "b", w2)
    assert by_id(steps)["T3"].raw.startswith("NOCALL")


def test_the_cleanup_line_is_scrubbed(tmp_path):
    w = World()
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        if argv[4] == T.REMOVE_SCRIPT:
            raise GuestError(1, f"stderr echoed {LIMITED}")
        return orig(argv, text, timeout)

    w.stdin_run = run
    _w, ev, (status, _s, path) = go(tmp_path, w)
    assert LIMITED not in (ev / "steps.log").read_text() and LIMITED not in path.read_text()


def test_a_second_run_claiming_the_same_dir_is_a_runbook_error(tmp_path, monkeypatch):
    ev = RB.Evidence(tmp_path)
    (tmp_path / "EXPECTED.txt").write_text("raced in")
    with pytest.raises(RB.RunbookError, match="claimed"):
        ev.write_expected("x")


def test_a_write_that_makes_no_progress_raises_instead_of_looping(tmp_path, monkeypatch):
    monkeypatch.setattr(RB.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        RB._write_durable(tmp_path / "f", "abc")


def test_a_foreign_json_new_is_not_deleted_after_an_ambiguous_t1(tmp_path):
    """Another credential writer's in-progress .json.new sits there at cleanup: it is left alone and the run is not a PASS."""
    w = World()
    w.foreign_new = "SOMEONE ELSE'S UPDATE"
    orig = w.stdin_run

    def run(argv, text, timeout=60):
        out = orig(argv, text, timeout)
        if argv[4] == T.INSTALL_SCRIPT:
            raise GuestError(255, "ssh dropped after the write")
        return out

    w.stdin_run = run
    _w, _ev, (status, _s, path) = go(tmp_path, w)
    assert w.new_removed == 0 and status == "INCOMPLETE"
    assert "left in place" in path.read_text() and "RESULT: PASS" not in path.read_text()
    assert any(LIMITED in t for t in w.stdins)       # the proof (our exact bytes) was what cleanup sent


def test_remove_new_script_unlinks_only_a_byte_equal_file(tmp_path):
    (tmp_path / "store" / "keys" / "cred").mkdir(parents=True)
    r, d = _sh(tmp_path, T.REMOVE_NEW_SCRIPT, "k", None, "mine")
    assert r.stdout.strip() == "cred-removed-new k"                         # nothing there: nothing to leave behind
    (d / "k.json.new").write_text("theirs")
    r, d = _sh(tmp_path, T.REMOVE_NEW_SCRIPT, "k", None, "mine")
    assert r.stdout.strip() == "cred-foreign-new k" and (d / "k.json.new").read_text() == "theirs"
    (d / "k.json.new").unlink()
    (d / "target").write_text("mine")
    os.symlink(d / "target", d / "k.json.new")
    r, d = _sh(tmp_path, T.REMOVE_NEW_SCRIPT, "k", None, "mine")
    assert r.stdout.strip() == "cred-foreign-new k" and os.path.islink(d / "k.json.new")   # a link is never ours
    os.unlink(d / "k.json.new")
    (d / "k.json.new").write_text("mine")
    r, d = _sh(tmp_path, T.REMOVE_NEW_SCRIPT, "k", None, "mine")
    assert r.stdout.strip() == "cred-removed-new k" and not (d / "k.json.new").exists()
