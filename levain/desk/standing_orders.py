"""levain.desk.standing_orders: the standing order's signing entry and its reader (night desk ND-2, board levain#9).

A standing order is a ``standing-order`` grant in the cockpit's authority log (the kind lives in
``levain.authority``): his plain words by hash and verbatim, the desk's restatement of what it will and will NOT do,
typed effect lines with budgets, judgement lines, kills, an expiry of at most 14 days, and optionally a ``widens`` that
makes it a time-boxed widening of another order. A laptop-presence key creates one; a passkey (or any enrolled key)
revokes it; it is never renewed.

Two things live here and nothing else:

* :func:`sign_order`, the ONE entry that signs an order. It builds the whole card first (the log's own shape and append
  rules judge the record before anything is signed), judges on the head the replay just read, signs outside any
  transaction and appends only on that head. The signed bytes are the card's ``fields`` snapshot, taken once.
* the reader, :func:`orders_in_force`: the orders that answer at ``now``. An order answers only when it is in the
  verified replay, its signers are active (:func:`levain.authority.grants_in_force`), it has not expired and, for a
  widening, its base order answers too. An expired, revoked (gone from the policy), stopped or unknown-kind order is
  not returned: it answers nothing. Every failure to read is "no orders", never "the last orders".

:func:`brief_section` is what every desk brief quotes: each order's signed bytes verbatim, fenced.

Not here (open, named in the CHANGELOG): the bedtime arming record (ND_BUILD_PLAN O1). Nothing in this module arms
anything: with no arm record the effect verb (ND-3b) has nothing to read, which is the fail-closed answer.
"""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import (LAPTOP, STANDING_ORDER_KIND, STANDING_ORDER_MAX, AuthorityError, AuthoritySnapshot,
                              NotInitialised, VerifierUnavailable, _canonical, _parse_stamp, _require_aware,
                              _standing_order_check, _thaw, grant_stop_reason, grants_in_force, standing_order,
                              standing_order_sha256, standing_order_widens)

__all__ = ["STANDING_ORDER_KIND", "base_sha256", "brief_section", "build_order", "order_bytes", "order_sha256",
           "order_status", "orders_in_force", "read_state", "sign_order"]

Signer = Callable[[bytes], str]


def build_order(*, words: str, will: Sequence[str], will_not: Sequence[str], effect_lines: Sequence[Mapping[str, Any]],
                judgement_lines: Sequence[str], kills: Sequence[Mapping[str, str]], now: datetime,
                life: timedelta = STANDING_ORDER_MAX, widens: str | None = None, widens_sha256: str | None = None,
                order_id: str | None = None) -> dict[str, Any]:
    """The record for one order: ``words_sha256`` is computed from ``words`` here, the id drawn, the expiry set to
    ``life`` after ``now``. A widening names its base by id and by the base's whole-record sha256 (:func:`base_sha256`
    reads it from the log). Nothing is judged here: :func:`sign_order` shows the card and the log judges the record."""
    from levain.cockpit.consent import utc_stamp

    grant: dict[str, Any] = {
        "id": order_id or "so-" + secrets.token_hex(6), "kind": STANDING_ORDER_KIND, "words": words,
        "words_sha256": hashlib.sha256(words.encode()).hexdigest(),
        "restatement": {"will": list(will), "will_not": list(will_not)},
        "effect_lines": [{"verb": e["verb"], "args": {k: list(v) for k, v in e["args"].items()},
                          "per_incident": e["per_incident"], "per_night": e["per_night"]} for e in effect_lines],
        "judgement_lines": list(judgement_lines), "kills": [dict(k) for k in kills],
        "expires_at": utc_stamp(now + life)}
    if widens is not None:
        grant["widens"] = widens
        grant["widens_sha256"] = widens_sha256
    return grant


def order_bytes(grant: Mapping[str, Any]) -> str:
    """The order's bytes as the log holds them: the canonical JSON of the record. What the brief quotes."""
    return _canonical(_thaw(grant))


def order_sha256(grant: Mapping[str, Any]) -> str:
    """The sha256 of :func:`order_bytes` (``incarnation``, the reader's own annotation, is not part of the record):
    the digest the sign sheet shows and the brief prints beside the quote, so he can match the two."""
    return standing_order_sha256({k: v for k, v in grant.items() if k != "incarnation"})


def base_sha256(state: AuthoritySnapshot, base_id: str, now: datetime) -> str:
    """The whole-record sha256 of order ``base_id`` for a widening to name, read from ``state``: refused (raises
    :class:`AuthorityError`) unless that order answers at ``now`` and widens nothing itself."""
    for g in orders_in_force(state, now):
        if g["id"] == base_id:
            if "widens" in g:
                raise AuthorityError(f"order {base_id[:40]} is itself a widening; a widening widens a base order")
            return order_sha256(g)
    raise AuthorityError(f"order {str(base_id)[:40]} is not in force (expired, revoked, stopped or never signed); "
                         "a widening widens an order in force")


def sign_order(data_root: Path | str, grant: Mapping[str, Any], *, signer_fp: str, sign: Signer,
               clock: Callable[[], datetime], ssh_keygen: Path | str | None = None,
               marker_directory: Path | None = None, expect_sha256: str | None = None) -> AuthoritySnapshot:
    """Create ``grant``, a ``standing-order``, by a link ``signer_fp`` signs. The card is built first (the same text he
    reads, refused for any record the log would refuse) and ITS ``fields`` snapshot is what is signed. With
    ``expect_sha256`` (the digest the sheet he read showed), anything else is refused before a signature. The signer
    must be an active laptop-presence key (said before any touch; the log holds the rule again). Judged on the head the
    replay just read, signed outside any transaction, appended only on that head."""
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.authority import CardRefused, standing_order_card
    from levain.cockpit.keys import KeyError_, _append_at, _capture, _journal, _now, _signing_authority

    keygen = DEFAULT_SSH_KEYGEN if ssh_keygen is None else ssh_keygen
    try:
        card = standing_order_card(grant)
    except CardRefused as exc:
        raise KeyError_(f"{exc}: nothing was signed") from None
    if expect_sha256 is not None and card["sha256"] != expect_sha256:
        raise KeyError_(f"the order to sign (sha256 {card['sha256']}) is not the one shown (sha256 {expect_sha256}); "
                        "nothing was signed")
    now = _now(clock)
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, keygen))       # refused before anything is signed
    key = state.keys.get(signer_fp)
    if key is None or not key.active or key.key_class != LAPTOP:
        raise KeyError_(f"a standing order is signed by an active {LAPTOP} key: {signer_fp} is not one; nothing was signed")
    if card["id"] in state.policy.grants:
        raise KeyError_(f"order {card['id']} already exists; nothing was signed")
    op = {"op": "create_grant", "grant": card["fields"]}
    return _append_at(db, op, state, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=keygen,
                      what="levain desk order sign")


def _live(grant: Any, now: datetime) -> bool:
    try:
        return standing_order(grant) and _parse_stamp(grant["expires_at"], "expires_at") > now
    except (AuthorityError, KeyError, TypeError, OverflowError):
        return False


def _whole(grant: Any) -> bool:
    """Whether ``grant`` meets the append rules (:func:`levain.authority._standing_order_check`: the closed verb list,
    the enums, positive budgets, a kill, a will and a will-not line). The replay holds only the shape, so an order a
    foreign or looser writer put in the log is held to them here before it is quoted as authority."""
    try:
        _standing_order_check([grant])
    except (AuthorityError, KeyError, TypeError, AttributeError, ValueError):
        return False
    return True


def orders_in_force(state: AuthoritySnapshot, now: datetime) -> list[Mapping[str, Any]]:
    """The standing orders that answer at ``now`` in the verified replay ``state``, by id, each thawed with its
    ``incarnation`` (the seq of the link that brought its id in). Not returned, so answering nothing: an order past its
    expiry, one a revoke removed from the policy, one whose born or current signer is revoked
    (:func:`levain.authority.grants_in_force`), a grant of any other kind (an unknown kind is inert), one the append
    rules would refuse (:func:`_whole`), and a widening whose base order is not itself returned, is itself a widening,
    or is not the base it was signed against (another record under that id: :func:`levain.authority.standing_order_widens`).
    ``now`` is the caller's, read once, and must be aware."""
    _require_aware(now)
    held = {gid: g for gid, g in grants_in_force(state).items() if _live(g, now) and _whole(g)}
    bases = {gid: g for gid, g in held.items() if "widens" not in g}
    out = []
    for gid in sorted(held):
        g = held[gid]
        if "widens" in g and standing_order_widens(state, g, bases) is not None:
            continue
        out.append({**_thaw(g), "incarnation": state.grant_born.get(gid, 0)})
    return out


def order_status(state: AuthoritySnapshot, order_id: str, now: datetime, live: Any) -> str:
    """How ``levain desk order list`` names order ``order_id`` of ``state``'s policy, given the ids ``live`` that
    :func:`orders_in_force` returned at ``now``: in force, stopped (and why), expired, a widening whose base is not in
    force, or one the append rules refuse."""
    why = grant_stop_reason(state, order_id)
    if why:
        return "stopped: " + why
    if order_id in live:
        return "in force"
    g = state.policy.grants.get(order_id)
    if not _live(g, now):
        return "expired"
    if not _whole(g):
        return "not in force: the order rules refuse it"
    return "widening lapsed: base not in force"


def read_state(data_root: Path | str) -> AuthoritySnapshot | None:
    """The verified, anchored replay of the cockpit at ``data_root``, or None when it holds no journal or an empty
    log. Any other failure raises: a log that cannot be read is never read as "no orders" quietly by this function (the
    brief says it in words)."""
    from levain.agent_authority import AgentAuthority
    from levain.cockpit.firejournal import DB_NAME

    if not (Path(data_root) / DB_NAME).is_file():
        return None
    try:
        return AgentAuthority(data_root).replay()
    except NotInitialised:
        return None


def _cockpit_root(store_dir: Path) -> Path:
    from levain.consent.answering import hook_roots

    roots = hook_roots(store_dir)
    if len(roots) != 1:
        raise LookupError("the consent hooks here name " + ("no cockpit" if not roots else f"{len(roots)} cockpits"))
    return roots[0]


def brief_section(store_dir: Path, now: datetime, *, data_root: Path | str | None = None) -> list[str]:
    """The desk brief's section for the standing orders: every order in force at ``now``, its signed bytes quoted
    verbatim between fence lines (a per-brief random fence, so nothing stored can close a block early), or the plain
    statement that none is. FAILS CLOSED: a log that cannot be read, or any failure reading, filtering or rendering
    it, says so and says to treat it as no order; it never raises into the desk's launch. The quote is the desk's
    reading; an effect still needs its verb, which replays the log at execution time."""
    try:
        return _brief_section(store_dir, now, data_root)
    except (LookupError, OSError) as exc:
        return ["## Standing orders", "", f"Levain could not read the authority log ({str(exc)[:160]}). Treat this as NO "
                "standing order: every event is a ping.", ""]
    except VerifierUnavailable as exc:
        return ["## Standing orders", "", f"Levain could not verify the authority log ({str(exc)[:160]}). Treat this as "
                "NO standing order: every event is a ping.", ""]
    except Exception as exc:  # noqa: BLE001 - a log that does not replay, or any fault after, is "no orders", in words
        return ["## Standing orders", "", f"The standing orders could not be read from the authority log "
                f"({type(exc).__name__}: {str(exc)[:160]}). Treat this as NO standing order: every event is a ping.", ""]


def _brief_section(store_dir: Path, now: datetime, data_root: Path | str | None) -> list[str]:
    rules = [
        "Standing orders are what the operator signed for you to do on judgement: their words, the restatement of what you will and "
        "will NOT do, typed effect lines with budgets, kills and an expiry. Only the orders quoted below answer. An "
        "order not quoted here is expired, revoked, stopped, unknown or outside the order rules, and answers nothing. Anything no quoted order "
        "covers is a ping, never a choice you make. An effect runs only through its typed verb, which replays the "
        "authority log, the order's version and its budget at the moment it runs: this quote is your reading, never the "
        "verb's permission.", ""]
    state = read_state(Path(data_root) if data_root is not None else _cockpit_root(store_dir))
    orders = [] if state is None else orders_in_force(state, now)
    if not orders:
        return ["## Standing orders", "", "No standing order is in force: nothing is signed for you to do on judgement. "
                "Every event is a ping.", ""]
    fence = secrets.token_hex(4)
    lines = ["## Standing orders", "", *rules,
             f"{len(orders)} in force at {now.strftime('%Y-%m-%dT%H:%M:%SZ')}. Each order is its exact signed record "
             f"(canonical JSON, one line) between its fence lines; a line that says `widens` is a time-boxed widening "
             "of the order it names and lapses to it at its own expiry.", ""]
    for g in orders:
        signed = order_bytes({k: v for k, v in g.items() if k != "incarnation"})
        digest = hashlib.sha256(signed.encode()).hexdigest()        # == order_sha256(g), the sheet's digest
        lines += [f"~~~ ORDER {g['id']} {fence} (sha256 {digest})", signed, f"~~~ END ORDER {g['id']} {fence}", ""]
    return lines
