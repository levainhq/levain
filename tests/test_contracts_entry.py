"""F9's FROZEN entry schema (S1_DESIGN rev 10 N1/N2): ``validate_entry_v1`` against the verdicts RECORDED in
frozen/entry/ at the freeze (generated there by ``entry.validate`` at levain 31dea1f), never against live ``team.entry``.

FROZEN (blocking): every recorded verdict (accept, or refuse plus the refusing ``raise`` site and its message); 100%
branch coverage of the copy by those vectors; the copy's sha256. DRIFT (non-blocking): the copy against live
``levain.team.entry.validate``; a divergence prints that T's schema changed and never fails.
"""
import hashlib
import json
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.contracts.leaves import entry_v1 as E
from tests import _branch_coverage as BC
from tests._contracts_vectors import FROZEN, check_shape, ids, load

VECTORS = load("entry")
DOC = json.loads((FROZEN / "entry" / "vectors.json").read_text(encoding="utf-8"))
COPY = Path(E.__file__)
# The copy's source, pinned. After the freeze an edit is a new (line, v), never an edit here. Moved once before the
# freeze, by S1_DESIGN rev 11 (re.ASCII on ID_RE and TS_RE; was bc97b031...e6d861).
COPY_SHA256 = "d1b2cf44d8de1b95b680b85ad25ea97ac869a056fb6cacd58e42ad2833c25e0f"
_LINES = COPY.read_text(encoding="utf-8").splitlines()
_START = next(i for i, ln in enumerate(_LINES, 1) if ln.startswith("def validate_entry_v1("))
SITES = [i for i in range(_START, len(_LINES) + 1) if "raise EntryError" in _LINES[i - 1]]


def _now(iso):
    return None if iso is None else datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def run_copy(i) -> dict:
    try:
        E.validate_entry_v1(i["entry"], i["known"], scan=i["scan"], now=_now(i["now"]))
    except E.EntryError as e:
        line = [f.lineno for f in traceback.extract_tb(e.__traceback__) if f.filename == str(COPY)][-1]
        return {"verdict": "refuse", "branch": SITES.index(line) + 1, "message": str(e)}
    return {"verdict": "accept"}


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    assert v["op"] == "validate_entry_v1"
    assert run_copy(v["input"]) == v["expect"]["levain"]


def test_the_copy_source_is_pinned():
    assert hashlib.sha256(COPY.read_bytes()).hexdigest() == COPY_SHA256


def test_every_raise_site_has_a_recorded_refusal():
    assert len(SITES) == DOC["raise_sites"] == 35
    assert {v["expect"]["levain"].get("branch") for v in VECTORS} - {None} == set(range(1, 36))


def test_one_boundary_accept_per_bounded_field():
    accepted = {v["id"] for v in VECTORS if v["expect"]["levain"]["verdict"] == "accept"}
    fields = [*E._MAX_LEN, "paths-100-items", "supersedes-100-items", "refs-100-items", "path-300-chars"]
    for f in fields:
        assert any(i.startswith(f"boundary-{f}") for i in accepted), f
    for f, n in E._MAX_LEN.items():
        v = next(v for v in VECTORS if v["id"] == f"boundary-{f}-{n}")
        assert len(v["input"]["entry"][f]) == n


def test_now_none_skips_exactly_the_future_branch():
    by = {v["id"]: v for v in VECTORS}
    assert by["r09-ts-future"]["expect"]["levain"]["branch"] == 9
    skip = by["now-none-skips-the-future-ts-branch"]
    assert skip["input"]["now"] is None and skip["input"]["entry"]["ts"] == by["r09-ts-future"]["input"]["entry"]["ts"]
    assert skip["expect"]["levain"] == {"verdict": "accept"}
    assert by["now-none-still-refuses-every-other-branch"]["expect"]["levain"]["branch"] == 13


def test_the_declared_ascii_divergence_moves_only_its_own_vectors():
    """Rev 11 (T03): 31dea1f accepted a fullwidth id / ts; the copy refuses them. Only vectors marked so differ."""
    moved = {v["id"]: v for v in VECTORS if "divergence" in v}
    assert set(moved) == {"r05-id-fullwidth-digits", "r07-ts-fullwidth-year", "r07-ts-fullwidth-digits",
                          "r07-ts-arabic-indic-year"}
    for v in moved.values():
        assert v["divergence"]["at_31dea1f"] != v["expect"]["levain"]
        assert v["expect"]["levain"]["verdict"] == "refuse" and "rev 11" in v["divergence"]["rule"]
    # measured: 31dea1f ACCEPTS a fullwidth id and a fullwidth-year ts (strptime's %Y takes them); the other two
    # passed its TS_RE and reached its real-date branch, a refusal for a different reason
    assert moved["r05-id-fullwidth-digits"]["divergence"]["at_31dea1f"] == {"verdict": "accept"}
    assert moved["r07-ts-fullwidth-year"]["divergence"]["at_31dea1f"] == {"verdict": "accept"}
    assert E.ID_RE.flags & __import__("re").ASCII and E.TS_RE.flags & __import__("re").ASCII


def test_now_is_required_never_read_from_the_clock():
    with pytest.raises(TypeError):
        E.validate_entry_v1(VECTORS[0]["input"]["entry"])     # no now: the caller must say ingest time or None


@pytest.mark.skipif(not BC.SUPPORTED, reason="branch coverage is measured on CPython 3.12 and 3.13 (the CI matrix)")
def test_the_vectors_reach_every_branch_of_the_copy():
    with BC.record(E.validate_entry_v1) as rec:
        for v in VECTORS:
            run_copy(v["input"])
    assert len(rec.sites) > 50
    assert rec.missing() == []


def test_drift_against_live_team_entry_is_informational(monkeypatch, capsys):
    """DRIFT, non-blocking: a divergence says T's schema changed and that it ships as a new (line, v)."""
    from levain.team import entry as live
    diverged = []
    for v in VECTORS:
        if "divergence" in v:    # a DECLARED divergence from 31dea1f (rev 11), not a T schema change
            continue
        i = v["input"]
        fixed = _now(i["now"] or "2099-01-02T00:00:00Z")

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed
        monkeypatch.setattr(live, "datetime", Clock)
        try:
            live.validate(json.loads(json.dumps(i["entry"])), i["known"], scan=i["scan"])
            got = "accept"
        except live.EntryError:
            got = "refuse"
        except Exception as exc:   # noqa: BLE001 - informational only
            got = f"error {type(exc).__name__}"
        if got != v["expect"]["levain"]["verdict"]:
            diverged.append(v["id"])
    if diverged:
        msg = f"T schema changed: a new `(line, v)` is needed ({len(diverged)} vectors: {', '.join(diverged[:10])})"
        print(msg)
        warnings.warn(msg, stacklevel=1)
