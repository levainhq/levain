"""TEST ONLY: coverage sources for the gate's graduation clamp, which no production configuration builds.

The gate runs a binding's effect, or an unattended manual effect, below CONFIRM only while a signed graduation
policy covers it (14b DESIGN_REV3 §4, DESIGN_REV3_1 D1/D4). The source that replays the authority log and reads
root's allowed-signers file is S4's; until then, and for the tests that drive the gate itself, this module gives:

- :class:`TestOnlyCoverage`: an in-memory authority state (a head that moves on every grant and revoke, root's
  file bytes, the replayed policies) behind the S2 ``CoverageSource`` contract: ``refresh`` builds a
  ``CoverageSnapshot`` from one read of that state and caches ``Uncovered`` at the token a peek reads when it
  fails; ``peek`` reads the root file's digest first and the head last, then compares in pure code.
- :func:`policy` and :func:`root_file`: a graduation policy record and a root file that lists its signer.
- :func:`cover_every_effect_for_this_test` (in a test's child interpreter,
  :func:`cover_every_effect_in_this_child_process`): for one test, a gate built without a source covers every
  effect the manifest declares at the rung it was decided at, which is what the gate did before the clamp
  existed. Only code that patches the module can do this (pytest's ``monkeypatch``, undone when the test ends).
"""
from __future__ import annotations

import base64
import datetime as _dt
import functools
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping

import pytest

import levain.autonomic.gate as _gate
from levain.autonomic.coverage import (
    Cover, CoverageSnapshot, CoverageView, Effect, EffectiveCover, GraduationPolicy, Uncovered, peek_cover_snapshot,
    risk_dict, signers_digest,
)
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk

STORE = "store-" + "5" * 32
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def test_key(seed: str) -> str:
    """A public key line ``ssh-ed25519 <base64>``, distinct per ``seed`` (no private half: coverage only
    compares keys; the signature over each link is the authority log's to verify)."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + hashlib.sha256(seed.encode()).digest()
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


LAPTOP = test_key("laptop")


def root_file(*keys: str, namespaces: str = "levain-authority") -> bytes:
    """Root's allowed-signers file listing ``keys`` in ``namespaces``."""
    return b"".join(f'operator namespaces="{namespaces}" {k}\n'.encode() for k in keys)


# A complete signed guard (every entry a rationale, a kill and a drill that trips it): what every policy is signed with
TEST_GUARD = ({"rationale": "a spoofed sender is the case this could be wrong about",
               "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
               "kill_drill": {"fields": {"dmarc": "fail"}}},)
NO_GUARD = object()   # policy(guard=NO_GUARD): a record signed before the guard (it covers nothing)


def terms_incarnation(record: Mapping, born: int) -> str:
    """The incarnation the authority log gives ``record`` brought in at seq ``born`` (``levain.authority.
    graduation_incarnation``'s formula)."""
    terms = {k: v for k, v in record.items() if k != "expires_at"}
    text = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{born}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def policy(gid: str, actions: Mapping[str, ActionRisk], *, expires_at: _dt.datetime,
           lowest: str = "ON_LOOP", signer: str = LAPTOP, renewer: str | None = None, store_id: str = STORE,
           triggers: list[str] | None = None, families: list[str] | None = None, born: int = 1,
           guard=TEST_GUARD, min_clean_fires: int | None = None) -> GraduationPolicy:
    """The replayed policy a ``create_grant`` of this §3 record by ``signer`` at seq ``born`` gives (``renewer``: the
    key of the renew that last moved its expiry), signed with ``guard`` (and ``min_clean_fires`` when given: the term
    ``levain.graduation_policy.TermedGraduationPolicy`` reads)."""
    record: dict = {"id": gid, "kind": "graduation", "lowest_posture": lowest, "store_id": store_id,
                    "expires_at": expires_at.astimezone(_dt.timezone.utc).strftime(_STAMP),
                    "actions": {name: risk_dict(risk) for name, risk in actions.items()}}
    if triggers is not None:
        record["triggers"] = triggers
    if families is not None:
        record["families"] = families
    if guard is not NO_GUARD:
        record["guard"] = [dict(g) for g in guard]
    if min_clean_fires is not None:
        record["min_clean_fires"] = min_clean_fires
    return GraduationPolicy.from_record(record, incarnation=terms_incarnation(record, born), born=born,
                                        born_signer=signer, current_signer=renewer or signer)


class TestOnlyCoverage:
    """An in-memory coverage source. ``grant``/``revoke`` move the head (an authority log append);
    ``set_root`` replaces root's file without moving it. ``fail_refresh`` makes every refresh fail as a
    replay that cannot run does. ``after_refresh`` runs once, right after the next refresh has read its state:
    a write, or time passing, between a decision and its admission."""

    __test__ = False   # not a pytest test class

    def __init__(self, policies: Iterable[GraduationPolicy] = (), *, root: bytes | None = None,
                 store_id: str = STORE) -> None:
        self.store_id = store_id
        self.root = root_file(LAPTOP) if root is None else root
        self._policies = {p.grant_id: p for p in policies}
        self._seq = 1 if self._policies else 0
        self._cached: CoverageView | None = None
        self.fail_refresh = False
        self.after_refresh: Callable[[], None] | None = None
        self.refreshes = 0
        self.peeks = 0

    @property
    def head(self) -> tuple[int, str]:
        return (self._seq, hashlib.sha256(f"head-{self._seq}".encode()).hexdigest()) if self._seq else (0, "")

    def grant(self, p: GraduationPolicy) -> None:
        self._policies[p.grant_id] = p
        self._seq += 1

    def revoke(self, gid: str) -> None:
        del self._policies[gid]
        self._seq += 1

    def set_root(self, root: bytes) -> None:
        self.root = root

    def refresh(self) -> CoverageView:
        self.refreshes += 1
        root = self.root                    # the root file's bytes, read once
        head = self.head                    # the replay's own head
        try:
            if self.fail_refresh:
                raise ValueError("replay failed")
            view: CoverageView = CoverageSnapshot(head, root, tuple(self._policies.values()), self.store_id)
        except (TypeError, ValueError):
            # a failed refresh: nothing is covered at the token a peek reads (S2's two error types, kept apart)
            view = Uncovered(self.head, signers_digest(self.root), self.store_id)
        self._cached = view
        hook, self.after_refresh = self.after_refresh, None
        if hook is not None:
            hook()
        return view

    def peek(self, effect: Effect, now: _dt.datetime) -> str:
        return self.peek_cover(effect, now).value

    def peek_cover(self, effect: Effect, now: _dt.datetime) -> EffectiveCover:
        self.peeks += 1
        cached = self._cached                # the cache once,
        digest = signers_digest(self.root)   # root first,
        head = self.head                     # the head last: then only pure comparison
        return peek_cover_snapshot(cached, (head, digest, self.store_id), effect, now)


# The guard of the policies that authorize _EveryDeclaredEffect's covers (1026-21 RULINGS O-g1-8: the fire check
# refuses a covered value no policy authorizes). Its kill is ``exists`` of a field no test event carries, so it lets
# every event through (``exists`` on an absent field is FALSE, never UNKNOWN), and its drill carries the field, so it
# trips.
_NEVER_FIELD = "levain_test_only_never_present"
EVERY_EFFECT_GUARD = ({"rationale": "test only: the gate covering every declared effect, as before the clamp",
                       "kill_predicate": {"op": "exists", "field": _NEVER_FIELD},
                       "kill_drill": {_NEVER_FIELD: True}},)
_EVERY_EFFECT_EXPIRES = _dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)


@functools.lru_cache(maxsize=None)
def _every_effect_policy(action_name: str, action_risk: ActionRisk) -> GraduationPolicy:
    # a policy signing exactly this action at exactly this risk (any trigger, any family), so the fire check's
    # ``covered`` holds for the effect it is read for and for no other action
    return policy("gp-test-only-every", {action_name: action_risk}, expires_at=_EVERY_EFFECT_EXPIRES,
                  guard=EVERY_EFFECT_GUARD)


class _EveryDeclaredEffect:
    """A view and source covering every effect whose action the manifest declares, at ON_LOOP: the rung the
    gate decided stands (the floor can still raise it), as before the clamp. Each cover names a policy signing
    that effect's action at its risk, whose guard trips on no test event."""

    token = ((0, ""), "", STORE)

    def cover(self, effect: Effect, now: _dt.datetime) -> Cover:
        if effect.action_risk is None:
            return Cover(None, "none")
        p = _every_effect_policy(effect.action_name, effect.action_risk)
        return Cover(Posture.ON_LOOP, p.cover_value, p)

    def refresh(self) -> CoverageView:
        return self

    def peek(self, effect: Effect, now: _dt.datetime) -> str:
        return self.cover(effect, now).value

    def peek_cover(self, effect: Effect, now: _dt.datetime) -> EffectiveCover:
        cover = self.cover(effect, now)
        return EffectiveCover(cover.value, cover.posture, cover.policy)


_EVERY_DECLARED_EFFECT = _EveryDeclaredEffect()   # one object: a policy swap hands the same source on


def _covering_init(real: Callable) -> Callable:
    @functools.wraps(real)
    def init(self, *args, coverage=None, **kwargs):
        real(self, *args, coverage=coverage if coverage is not None else _EVERY_DECLARED_EFFECT, **kwargs)
    return init


def cover_every_effect_for_this_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """For this test only, a gate constructed without a coverage source covers every declared effect. (The
    patch wraps the constructor; it raises if the class is renamed, so a seam that stopped applying cannot
    pass for one that works.)"""
    monkeypatch.setattr(_gate.EfferentGate, "__init__", _covering_init(_gate.EfferentGate.__init__))


def cover_every_effect_in_this_child_process() -> None:
    """The same, for the rest of a test's own child interpreter (a crash test's fresh process), which has no
    ``monkeypatch``: the process ends with the test."""
    _gate.EfferentGate.__init__ = _covering_init(_gate.EfferentGate.__init__)


@pytest.fixture
def test_only_every_effect_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    cover_every_effect_for_this_test(monkeypatch)
