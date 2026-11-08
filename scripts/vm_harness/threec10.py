"""3c-10 (R4_DESIGN section 5): `levain doctor` against a real OpenRouter key with and without a limit, one real brokered call
settled from real usage, lineage dollars against the provider's own books, and an overrun stop on a real key. Host side. usage:
  LEVAIN_3C10_KEY_LIMITED=... LEVAIN_3C10_KEY_UNLIMITED=... \\
  python3 -m scripts.vm_harness.threec10 <vm> <evidence-dir> --install /path/of/the/install [--profile darwin|linux]
        [--cred or-key] [--levain-root /tmp/lvclean] [--call "CMD ARGS"] [--overrun-call "CMD ARGS"]
(the two keys are read from the environment, never from an argument; each is the provider's secret with a spend limit, and one
without. ``--call`` / ``--overrun-call`` are the guest commands that make ONE brokered call / ONE call whose real usage exceeds
its share (the dispatch fire the sitting uses; each is one quoted string, split shell-style and run as that argv).) Monday's sitting runs this one command; ``tests/test_vm_harness_threec10.py`` proves it end to end against
a FAKE guest and a FAKE provider, no VM and no vendor.

The steps, each with ONE expected verdict (:data:`EXPECTED`), written to ``EXPECTED.txt`` before the first guest call:
  T1  install the limited key as the keys credential (stdin; the secret is never in an argv)   -> ``cred-installed <cred>``
  T2  ``levain doctor`` as root: the ``invoice ceiling (<cred>)`` row reads the limit            -> ``key limit $X total, $Y remaining ...``
  T3  one real call: the provider's ``usage`` delta equals the lineage's settled dollars, within a stated tolerance -> ``AGREE ...``
  T4  an overrun: a lineage row with ``overrun`` set (the session stopped)                        -> ``overrun-row ...``
  T5  install the unlimited key                                                                   -> ``cred-installed <cred>``
  T6  ``levain doctor``: the same row says there is no limit                                      -> ``no credit limit is set ...``
What a verdict PROVES, exactly: the raw line of T3 and T4 is synthesized by this runner (T3 from the lineage rows the call
added, the provider's usage delta and the tolerance; T4 from the new lineage rows and the call's exit code), so the regex match
is a format check. The real verdict is the Python check that chose the word: T3 ``AGREE`` = a settled lineage row exists, the
provider's usage MOVED, and the two totals are within tolerance (an unmoved provider is ``NOMOVE``, never an agreement, however
small the lineage total); T4 ``overrun-row`` = a NEW lineage row appeared during that call carrying an integer ``overrun``
(it does not prove the session was killed, only that the sink recorded an overrun). T1/T5 install refuse an existing credential
and an empty one (``cred-refused``; the run ends REFUSED, exit 2). A step that timed out or lost its transport mid-run is ERROR
(no verdict observed): the run ends INCOMPLETE (exit 3).
The credential file is removed on the way out, and the removal is a GRADED result: a removal that times out, exits nonzero
or whose absence cannot be confirmed afterwards makes the run at least INCOMPLETE (exit nonzero) and RESULT.txt names it. An
install whose result is AMBIGUOUS (a transport error after the script may have run) never authorises replacing or deleting
``<cred>.json``: only ``<cred>.json.new`` is cleaned, T5 is not attempted, and the run is INCOMPLETE with the reason. Only the
script's own ``cred-installed`` line makes the file ours to replace and remove. A step whose input is missing (a key not in the environment, no
``--call``) is UNBOUND and the run ends INCOMPLETE, never PASS.

ASSUMED, not verified against a live account (the sitting is the first time): OpenRouter's ``GET /api/v1/key`` answers ``data.usage``
(USD, a float) beside ``limit`` / ``limit_remaining``; the guest operator can run ``levain doctor`` under ``sudo -n``; ``keys`` reads
a replaced credential file on its next exchange. A first-run mismatch there is a harness finding, not a product one.
Known edges (found by L3 round 1, deliberately not built):
  * the credential is written as root with ``cat >`` / ``chown``; a symlink planted at ``<cred>.json.new`` by the ``_levain_keys``
    service user (levain's own service user, on our own clone) would be followed. No credential-install verb exists to reuse.
  * the lineage sink and the provider key are read by row count / usage delta: a second session or a second harness using the
    same key or store during a run can offset T3 or fake T4. A VM clone runs one harness at a time.
  * ``urllib`` copies the Authorization header across a redirect; openrouter.ai does not redirect ``GET /api/v1/key``.
  * T2 right after T1 reads ``keys``' reload on its next exchange (assumed above); T3 stops polling at the first usage move.
R4 section 6 residue NOT scripted here (read, not run): whether 1.5 s is enough for a real limit read (D1 replaced that verb with
keys' own refresher), usage per codec (`include_usage`, `usage.cost` vs the class price, `cache_write_tokens`), a 429 retry storm at
its holds, a Responses stream over the 1 MiB event cap, the 32 MiB parse timing and the 64 KiB chunk bound over real TLS.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, PROFILES, HostProfile, profile_named
from scripts.vm_harness.journey8 import LEVAIN_ROOT, StepResult
from scripts.vm_harness.runbook import (Evidence, RunbookError, Verdict, capture, judge, overall, pick_line)

KEY_LIMITED_ENV = "LEVAIN_3C10_KEY_LIMITED"
KEY_UNLIMITED_ENV = "LEVAIN_3C10_KEY_UNLIMITED"
OPENROUTER_HOST = "openrouter.ai"
KEY_URL = "https://openrouter.ai/api/v1/key"
CRED_FORMAT = "levain-keys-credential/1"          # levain.broker.creds.FORMAT (a test pins them equal)
KEYS_USER = "_levain_keys"                          # levain.slot.layout's owner of <store>/keys
USD = r"\$-?[\d,]+\.\d\d"
CRED_NAME_RE = re.compile(r"[a-z0-9-]{1,48}")       # levain.broker.creds.NAME_RE

#: ``$3`` is ``new`` (refuse when the credential already exists: a file of his is never overwritten) or ``replace`` (the
#: sitting's own T1 file, swapped for the unlimited key). An empty credential is refused before it can be moved into place.
#: A refusal prints ``cred-refused ...`` and exits 0, so the runner reads it as data.
INSTALL_SCRIPT = ('set -e; d="$1/keys/cred"; n="$2"; umask 077; '
                  'if [ "$3" != replace ] && [ -e "$d/$n.json" ]; then cat > /dev/null; '
                  'echo "cred-refused $n: $d/$n.json already exists"; exit 0; fi; '
                  'cat > "$d/$n.json.new"; '
                  'if [ ! -s "$d/$n.json.new" ]; then rm -f "$d/$n.json.new"; echo "cred-refused $n: empty credential"; exit 0; fi; '
                  'chown ' + KEYS_USER + ':' + KEYS_USER + ' "$d/$n.json.new"; mv "$d/$n.json.new" "$d/$n.json"; '
                  'echo "cred-installed $n"')
#: Remove ONLY the half-written file: what an uncertain install leaves behind that is surely ours. Never ``<cred>.json``.
REMOVE_NEW_SCRIPT = 'set -e; rm -f "$1/keys/cred/$2.json.new"; echo "cred-removed-new $2"'
#: ``$3`` is ``all`` (the credential and its half-written twin) or ``new`` (the twin only); prints ``cred-absent`` or ``cred-present``.
VERIFY_SCRIPT = ('d="$1/keys/cred"; if [ "$3" = all ] && [ -e "$d/$2.json" ]; then echo "cred-present $2.json"; exit 0; fi; '
                 'if [ -e "$d/$2.json.new" ]; then echo "cred-present $2.json.new"; exit 0; fi; echo "cred-absent $2"')
REMOVE_SCRIPT = 'set -e; rm -f "$1/keys/cred/$2.json" "$1/keys/cred/$2.json.new"; echo "cred-removed $2"'

#: (id, name, rc, pattern with <cred>, line selector): the verdicts, one each, fixed before any run.
EXPECTED: tuple[tuple[str, str, int | None, str, str | None], ...] = (
    ("T1", "install the limited key", 0, r"cred-installed <cred>", r"^cred-installed "),
    ("T2", "doctor, key with a limit", None,
     r"\s*\S+ invoice ceiling \(<cred>(?: at [^)]+)?\): key limit " + USD + r" total, " + USD + r" remaining.*",
     r"invoice ceiling \(<cred>"),
    ("T3", "one real call settles from real usage", None, r"AGREE .*", r"^(AGREE|DISAGREE|NOCALL|NOMOVE) "),
    ("T4", "an overrun stops the session", None, r"overrun-row .*", r"^(overrun-row|no-overrun-row) "),
    ("T5", "install the unlimited key", 0, r"cred-installed <cred>", r"^cred-installed "),
    ("T6", "doctor, key with no limit", None,
     r"\s*\S+ invoice ceiling \(<cred>(?: at [^)]+)?\): no credit limit is set on this key: nothing at the provider "
     r"bounds the invoice.*", r"invoice ceiling \(<cred>"),
)

class FetchError(RunbookError):
    """The provider's answer could not be had or is not a number: no verdict was observed (a step ERROR, never a FAIL)."""


Fetch = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]


def default_fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """The one place this script reaches a real provider: a GET of the key's status with the key. Only a real run uses it."""
    import http.client
    import urllib.error
    import urllib.request
    if not url.startswith("https://" + OPENROUTER_HOST + "/"):
        raise RunbookError(f"refusing a request to {url!r}: only {OPENROUTER_HOST}")
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - the host is checked above
            return r.status, r.read(64 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as e:
        raise FetchError(f"the provider could not be reached: {type(e).__name__}: {e}") from None


def usage_microdollars(fetch: Fetch, key: str, timeout: float = 15) -> int:
    """The provider's own total spend on ``key``, in microdollars (``data.usage`` is US dollars)."""
    status, body = fetch(KEY_URL, {"Authorization": "Bearer " + key}, timeout)
    if status != 200:
        raise RunbookError(f"the provider's key status answered {status}")
    try:
        usage = json.loads(body)["data"]["usage"]
    except (ValueError, KeyError, TypeError):
        raise RunbookError("the provider's key status has no data.usage") from None
    if isinstance(usage, bool) or not isinstance(usage, (int, float)) or usage < 0:
        raise RunbookError(f"the provider's data.usage is not an amount: {usage!r}")
    if not math.isfinite(usage):
        raise FetchError(f"the provider's data.usage is not finite: {usage!r}")
    return round(usage * 1_000_000)


def credential_json(cred: str, key: str) -> str:
    """The keys credential file for a static OpenRouter key (the shape ``levain.broker.creds.parse`` reads)."""
    if not CRED_NAME_RE.fullmatch(cred):
        raise RunbookError(f"credential name {cred!r} is not [a-z0-9-]{{1,48}}")
    return json.dumps({"format": CRED_FORMAT, "name": cred, "rev": secrets.token_hex(16), "hosts": [OPENROUTER_HOST],
                       "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "static", "secret": key})


def lineage_rows(guest, profile: HostProfile) -> list[dict]:
    """The settled-spend sink as root reads it (``<store>/lineage/lineage.jsonl``); a row that is not JSON is skipped."""
    lines, rc = capture(guest, ("sudo", "-n", "/bin/cat", f"{profile.store}/lineage/lineage.jsonl"))
    if rc not in (0, 1):
        raise RunbookError(f"reading the lineage sink exited {rc}")
    rows = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


@dataclass
class Config:
    profile: HostProfile
    install: str
    cred: str = "or-key"
    levain_root: str = LEVAIN_ROOT
    call: tuple[str, ...] = ()
    overrun_call: tuple[str, ...] = ()
    keys: Mapping[str, str | None] | None = None        # {"limited": ..., "unlimited": ...}
    tolerance_pct: float = 5.0
    tolerance_floor_microdollars: int = 1000             # $0.001: provider rounding on a tiny call
    settle_wait_s: float = 90.0
    settle_poll_s: float = 5.0


def expected_text(cfg: Config) -> str:
    out = [f"3c-10: expected verdicts, os={cfg.profile.name}, cred={cfg.cred}", "step | name | rc | expected raw line (regex)"]
    for sid, name, rc, pat, _sel in EXPECTED:
        out.append(f"{sid} | {name} | {'any' if rc is None else rc} | /{pat.replace('<cred>', re.escape(cfg.cred))}/")
    out.append(f"T3 tolerance: |lineage - provider| <= max({cfg.tolerance_pct}% of the provider delta, "
               f"{cfg.tolerance_floor_microdollars} microdollars)")
    return "\n".join(out)


def _doctor_argv(cfg: Config) -> tuple[str, ...]:
    return ("sudo", "-n", "/usr/bin/env", f"PYTHONPATH={cfg.levain_root}", cfg.profile.python, "-m", "levain", "doctor",
            "--path", cfg.install)


def run_3c10(guest, stdin_run, evidence_dir, cfg: Config, *, fetch: Fetch = default_fetch,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
    """Run T1-T6 through ``guest`` (``guest(argv, timeout) -> stdout``) and ``stdin_run`` (``(argv, stdin_text, timeout) ->
    stdout``), the provider through ``fetch``. Returns ``(status, [StepResult], result_path)``. The credential file is removed
    in a ``finally``; every line written to the evidence dir has every key scrubbed from it."""
    keys = dict(cfg.keys or {})
    secrets_in_play = [k for k in keys.values() if k]
    if not CRED_NAME_RE.fullmatch(cfg.cred):
        raise RunbookError(f"credential name {cfg.cred!r} is not [a-z0-9-]{{1,48}}")
    for sid, _n, _rc, pat, sel in EXPECTED:
        Verdict(_rc, pat).check()
    if not (math.isfinite(cfg.tolerance_pct) and cfg.tolerance_pct >= 0):
        raise RunbookError(f"--tolerance-pct must be a finite number >= 0, not {cfg.tolerance_pct!r}")
    ev = Evidence(evidence_dir)

    def scrub(text: str) -> str:
        for k in secrets_in_play:
            text = text.replace(k, "***")
        return text

    digest = ev.write_expected(expected_text(cfg))
    verdicts = {sid: (Verdict(rc, pat.replace("<cred>", re.escape(cfg.cred))), (sel or "").replace("<cred>", re.escape(cfg.cred)) or None)
                for sid, _n, rc, pat, sel in EXPECTED}
    names = {sid: name for sid, name, *_ in EXPECTED}
    results: dict[str, StepResult] = {}

    def done(sid: str, status: str, raw: str = "", detail: str = "") -> None:
        v = verdicts[sid][0]
        r = StepResult(sid, names[sid], status, scrub(raw), f"rc={'any' if v.rc is None else v.rc} /{v.pattern}/", scrub(detail))
        results[sid] = r
        ev.log(r.line())

    def unbound(sid: str, why: str) -> None:
        done(sid, "UNBOUND", detail=why)

    def judged(sid: str, lines: Sequence[str], rc: int, detail: str = "") -> None:
        v, sel = verdicts[sid]
        ok, raw = judge(lines, rc, v, sel)
        done(sid, "PASS" if ok else "FAIL", raw, detail if ok else (detail + f" rc={rc}").strip())

    #: ``none`` (nothing of ours is on the guest), ``confirmed`` (the script's own ``cred-installed`` line was seen: the file
    #: is ours to replace and to remove), ``uncertain`` (the script may have run and its answer was lost: ``<cred>.json`` is
    #: NEVER replaced or removed, only the ``.json.new`` twin).
    state = "none"
    uncertain_why = ""
    refused = False
    cleanup_problems: list[str] = []

    def install(sid: str, which: str) -> bool:
        nonlocal state, refused, uncertain_why
        key = keys.get(which)
        if refused:
            unbound(sid, "an earlier install was refused; nothing more is installed")
            return False
        if state == "uncertain":
            unbound(sid, "an earlier install's result is uncertain: not touching the credential file")
            return False
        if not key:
            unbound(sid, f"the {which} key is not in the environment")
            return False
        mode = "replace" if state == "confirmed" else "new"   # replace only a file whose install was CONFIRMED to be ours
        argv = ("sudo", "-n", "/bin/sh", "-c", INSTALL_SCRIPT, "sh", cfg.profile.store, cfg.cred, mode)
        attempted_from = state
        try:
            out = stdin_run(argv, credential_json(cfg.cred, key), 60)
        except (GuestError, OSError, subprocess.SubprocessError) as e:
            why = f"{sid}: no answer from the install script ({type(e).__name__}: {e})"
            if attempted_from == "confirmed":
                done(sid, "ERROR", detail=f"transport: {e}; the file there is the sitting's own either way")
            else:
                state, uncertain_why = "uncertain", why
                done(sid, "ERROR", detail=f"transport: {e}; install result UNCERTAIN, {cfg.cred}.json will not be touched")
            return False
        lines = out.splitlines()
        refusal = next((ln for ln in lines if ln.startswith("cred-refused ")), None)
        if refusal:
            refused = True        # nothing of ours was written: a file that was there is never removed
            done(sid, "REFUSED", detail=refusal)
            return False
        if f"cred-installed {cfg.cred}" in lines:
            state = "confirmed"
        else:
            if attempted_from != "confirmed":
                state, uncertain_why = "uncertain", f"{sid}: the install script printed neither cred-installed nor cred-refused"
            done(sid, "ERROR", raw=lines[-1] if lines else "", detail="no cred-installed line: install result UNCERTAIN")
            return False
        judged(sid, lines, 0)
        return results[sid].status == "PASS"

    def doctor(sid: str) -> None:
        try:
            lines, rc = capture(guest, _doctor_argv(cfg), 180)
        except (GuestError, RunbookError, OSError) as e:
            done(sid, "FAIL", detail=f"transport: {e}")
            return
        except subprocess.SubprocessError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return
        judged(sid, lines, rc)

    def settled_call(sid: str, argv: Sequence[str], limited: str) -> None:
        """T3: provider usage before, the call, provider usage after (polled until it moves), the lineage rows it added."""
        try:
            before_rows = len(lineage_rows(guest, cfg.profile))
            u0 = usage_microdollars(fetch, limited)
            lines, rc = capture(guest, argv, 600)
            if rc != 0:
                done(sid, "FAIL", raw=lines[-1] if lines else "", detail=f"the call exited {rc}")
                return
            deadline, u1 = clock() + cfg.settle_wait_s, u0
            while True:
                u1 = usage_microdollars(fetch, limited)
                if u1 != u0 or clock() >= deadline:
                    break
                sleep(cfg.settle_poll_s)
            new = lineage_rows(guest, cfg.profile)[before_rows:]
        except FetchError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return
        except (GuestError, RunbookError, OSError) as e:
            done(sid, "FAIL", detail=f"{type(e).__name__}: {e}")
            return
        except subprocess.SubprocessError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return
        settled = [r for r in new if type(r.get("cost_microdollars")) is int and r.get("model_answered") is not None]
        lin = sum(r["cost_microdollars"] for r in settled)
        prov = u1 - u0
        tol = max(cfg.tolerance_pct / 100.0 * prov, cfg.tolerance_floor_microdollars)
        if not settled:
            word = "NOCALL"
        elif u1 == u0 or prov <= 0:
            word = "NOMOVE"       # the provider's books never moved: a small lineage total is inside any floor, so NOT an agreement
        else:
            word = "AGREE" if abs(lin - prov) <= tol else "DISAGREE"
        raw = f"{word} lineage_microdollars={lin} provider_delta_microdollars={prov} rows={len(settled)} tolerance={int(tol)}"
        judged(sid, [raw], 0)

    def overrun(sid: str, argv: Sequence[str]) -> None:
        try:
            before_rows = len(lineage_rows(guest, cfg.profile))
            lines, rc = capture(guest, argv, 600)
            new = lineage_rows(guest, cfg.profile)[before_rows:]
        except (GuestError, RunbookError, OSError) as e:
            done(sid, "FAIL", detail=f"{type(e).__name__}: {e}")
            return
        except subprocess.SubprocessError as e:
            done(sid, "ERROR", detail=f"{type(e).__name__}: {e}")
            return
        hits = [r for r in new if type(r.get("overrun")) is int]
        raw = (f"overrun-row run_id={hits[0].get('run_id')} overrun={hits[0]['overrun']} settled={hits[0].get('settled')} "
               f"call_rc={rc}" if hits else f"no-overrun-row rows={len(new)} call_rc={rc}")
        judged(sid, [raw], 0)

    def cleanup() -> list[str]:
        """Remove what is surely ours and CONFIRM it is gone. Returns the problems (empty: clean)."""
        if state == "none":
            return []
        confirmed = state == "confirmed"
        script, label, vmode = (REMOVE_SCRIPT, "removed", "all") if confirmed else (REMOVE_NEW_SCRIPT, "removed-new", "new")
        base = ("sudo", "-n", "/bin/sh", "-c")
        tail = ("sh", cfg.profile.store, cfg.cred)
        problems: list[str] = []
        if not confirmed:
            problems.append(f"{uncertain_why}; {cfg.profile.store}/keys/cred/{cfg.cred}.json may be the sitting's credential or "
                            f"one of his: check it by hand, it was NOT touched")
        try:
            out = stdin_run(base + (script,) + tail, "", 60)
            if f"cred-{label} {cfg.cred}" not in out.splitlines():
                problems.append(f"removal of {cfg.cred} printed no cred-{label} line")
            seen = stdin_run(base + (VERIFY_SCRIPT,) + tail + (vmode,), "", 60)
            if f"cred-absent {cfg.cred}" not in seen.splitlines():
                problems.append(f"{cfg.cred} is STILL on the guest after removal ({seen.strip()[:80]})")
        except (GuestError, OSError, subprocess.SubprocessError) as e:
            problems.append(f"credential {cfg.cred} NOT confirmed removed ({type(e).__name__}: {e}); remove "
                            f"{cfg.profile.store}/keys/cred/{cfg.cred}.json* by hand")
        for p in problems:
            ev.log(scrub("cleanup: " + p))
        if not problems:
            ev.log(f"cleanup: credential {cfg.cred} removed and confirmed absent")
        return problems

    try:
        if install("T1", "limited"):
            doctor("T2")
            if cfg.call:
                settled_call("T3", cfg.call, keys["limited"])             # type: ignore[arg-type]
            else:
                unbound("T3", "no --call: the guest command that makes one brokered call is the sitting's to name")
            if cfg.overrun_call:
                overrun("T4", cfg.overrun_call)
            else:
                unbound("T4", "no --overrun-call: a run whose real usage exceeds its share is the sitting's to name")
        else:
            for sid in ("T2", "T3", "T4"):
                unbound(sid, f"T1 did not pass (install state: {state})")
        if install("T5", "unlimited"):
            doctor("T6")
        else:
            unbound("T6", "T5 did not install the unlimited key")
    finally:
        cleanup_problems += cleanup()
    ordered = [results[sid] for sid, *_ in EXPECTED]
    status = overall([r.status for r in ordered])
    if cleanup_problems and status == "PASS":
        status = "INCOMPLETE"     # a live credential left behind never sits beside a PASS
    body = [f"RESULT: {status}", f"3c-10, os={cfg.profile.name}, expected-table sha256 {digest} (EXPECTED.txt, written before the first step)"]
    body += [r.line() for r in ordered]
    body += [scrub(f"CLEANUP: {p}") for p in cleanup_problems] or ["CLEANUP: ok"]
    path = ev.write_result("\n".join(body) + "\n")
    return status, ordered, path


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("evidence")
    ap.add_argument("--install", required=True, help="the install dir `levain doctor --path` checks")
    ap.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    ap.add_argument("--cred", default="or-key")
    ap.add_argument("--levain-root", default=LEVAIN_ROOT)
    ap.add_argument("--call", default="", help="the guest command (one quoted string, split shell-style) that makes ONE brokered call")
    ap.add_argument("--overrun-call", default="", help="the guest command that makes a call whose real usage exceeds its share")
    ap.add_argument("--tolerance-pct", type=float, default=5.0)
    a = ap.parse_args(argv)
    profile = profile_named(a.profile)
    cfg = Config(profile, a.install, a.cred, a.levain_root, tuple(shlex.split(a.call)), tuple(shlex.split(a.overrun_call)),
                 {"limited": os.environ.get(KEY_LIMITED_ENV), "unlimited": os.environ.get(KEY_UNLIMITED_ENV)},
                 tolerance_pct=a.tolerance_pct)
    from scripts.vm_harness import vm_run
    from scripts.vm_harness.driver import AdapterError, VmAdapters
    from scripts.vm_harness.host_profile import ProfileMismatch
    from scripts.vm_harness.runbook import make_stdin_runner

    vm_run._clone_guard(a.vm)
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = vm_run.make_transport(a.vm, profile, Path(td))
        try:
            VmAdapters(guest, spawn, profile=profile).verify_host()
        except (ProfileMismatch, AdapterError) as e:
            print(f"threec10: refusing: {e}", file=sys.stderr)
            return 2
        try:
            stdin_run = make_stdin_runner(a.vm, profile, Path(td), guest.kind)     # the transport make_transport SELECTED
            status, _steps, path = run_3c10(guest, stdin_run, a.evidence, cfg)
        except RunbookError as e:
            print(f"threec10: refusing: {e}", file=sys.stderr)
            return 2
    print(path.read_text())
    return {"PASS": 0, "FAIL": 1, "REFUSED": 2}.get(status, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
