"""levain.dispatch.calibration — the label layer for a decision model (design F8, slice a).

A decision model (a "judge") may read a gated request and say allow or deny. It never authorises: nothing here, and
nothing built on it, turns a judge's word into a decision. What this module does is measure how often a candidate judge
agrees with the operator's own signed record, per action class, so that a grant PROPOSAL on the M17 card can rest on a
stated N and not on a feeling.

Labels are read from the consent queue through its public read-only reader, :func:`levain.consent.listing.read_listing`
(``mode=ro``, seals re-checked, nothing created or swept). One label per decided request:

* ``allow`` / ``deny``: the request's state is allowed or denied, decided by a SIGNED act: an answer (``via`` signature)
  or a late ruling (``via`` ruling). The operator's judgment, on that one call.
* ``undecided``: the hold ended with no answer and no ruling yet. Its own class, never a label in either direction: there
  is no human judgment on it. One later ruled becomes an allow or deny label by its ruling.
* a standing grant's allow (``via`` grant) is EXCLUDED and only counted: a grant is a judgment made once, ahead of the
  call, over a class of calls. It is not a fresh human judgment on this call, and counting it would score the judge
  against a rule the operator already wrote.
* expired and withdrawn requests went to the agent's own dialog; the queue holds no human verdict on them, so they are
  not labels either.

Each label carries its action class, ``(agent, tool_name, tier)``, the request id and when it was decided.

The honest limit is stated once, in :data:`CAVEAT`, and repeated by every report that prints a number. Stdlib only.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

from levain.consent.listing import ConsentEntry, read_listing
from levain.dispatch import DispatchError

__all__ = ["ActionClass", "CAVEAT", "ClassReport", "DEFAULT_SPAN", "Label", "LabelSet", "PREDICTIONS", "agreement",
           "deny_rate", "proposal_eligible", "read_labels"]

CAVEAT = ("Labels exist only for what was asked: a call the gate never held has no label. They include stamps: a human "
          "allow can be a rubber stamp. So agreement measures agreement with his record, not correctness.")

PREDICTIONS: tuple[str, ...] = ("allow", "deny", "abstain")
DEFAULT_SPAN = timedelta(days=90)
_NO_CAP = 10 ** 9                       # read_listing bounds its decided rows; a calibration wants them all

ActionClass = tuple[str, str, str]      # (agent, tool_name, tier)


@dataclass(frozen=True)
class Label:
    """One decided (or undecided) request. ``decision`` is ``allow``, ``deny`` or ``undecided``; ``via`` is
    ``signature`` (an answer) or ``ruling`` (a late ruling), ``None`` for an undecided one."""
    request_id: str
    action_class: ActionClass
    decision: str
    via: str | None
    decided_at: str | None


@dataclass(frozen=True)
class LabelSet:
    """What :func:`read_labels` found: the labels, how many grant-answered requests were left out and why they were
    (see the module docstring), and how many rows the reader skipped because they did not check out."""
    labels: tuple[Label, ...] = ()
    excluded_grants: int = 0
    skipped: int = 0


def _class_of(entry: ConsentEntry) -> ActionClass:
    r = entry.request
    return (r.agent, r.tool_name, r.tier)


def read_labels(directory: Path | str | None = None, *, now: datetime, span: timedelta = DEFAULT_SPAN) -> LabelSet:
    """The labels in the consent queue at ``directory`` (default: the queue's own directory) decided within ``span``
    of ``now``, plus every undecided request however old. Read-only through :func:`read_listing`; a missing queue is
    an empty set."""
    listing = read_listing(directory, now=now, span=span, cap=_NO_CAP)
    labels: list[Label] = []
    grants = 0
    for entry in (*listing.recent, *listing.undecided):
        if entry.state == "undecided":
            labels.append(Label(entry.request.id, _class_of(entry), "undecided", None, entry.decided_at))
        elif entry.state in ("allowed", "denied"):
            if entry.via == "grant":
                grants += 1
            elif entry.via in ("signature", "ruling"):
                labels.append(Label(entry.request.id, _class_of(entry), "allow" if entry.state == "allowed" else "deny",
                                    entry.via, entry.decided_at))
            # an allowed or denied row with any other via carries no signed judgment this layer can name: left out
    labels.sort(key=lambda lab: (lab.decided_at or "", lab.request_id))
    return LabelSet(tuple(labels), grants, listing.skipped)


# --- deny rate -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DenyRate:
    """``n`` allow and deny labels (undecided are NOT in it), ``denies`` of them denies, ``undecided`` kept apart.
    ``rate`` is exact, ``None`` with no label to divide by."""
    n: int
    denies: int
    undecided: int

    @property
    def rate(self) -> Fraction | None:
        return Fraction(self.denies, self.n) if self.n else None


def deny_rate(labels: Iterable[Label], *, by_class: bool = True) -> dict[object, DenyRate]:
    """How often he denied, with N: per action class (``by_class``) or under the single key ``"all"``. The
    always-allow judge's score is ``1 - rate``; :func:`agreement` states that baseline per class."""
    tally: dict[object, list[int]] = {}
    for lab in labels:
        key: object = lab.action_class if by_class else "all"
        n, denies, undecided = tally.setdefault(key, [0, 0, 0])
        if lab.decision == "undecided":
            tally[key][2] = undecided + 1
        else:
            tally[key][0] = n + 1
            tally[key][1] = denies + (lab.decision == "deny")
    return {key: DenyRate(*vals) for key, vals in tally.items()}


# --- agreement -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassReport:
    """One action class's agreement between a judge and the operator's record.

    ``n_labelled`` allow and deny labels; ``n_predicted`` the labelled requests the judge answered (not abstained);
    ``agree`` of those; ``judge_allow_human_deny`` the dangerous direction (the judge would have let through what he
    denied), ``judge_deny_human_allow`` the costly-but-safe one. ``abstained`` labelled requests the judge passed on;
    ``deny_rate`` his rate over every labelled request of the class. ``baseline_agreement`` is what an always-allow judge
    would have scored over the SAME predicted requests (his allows among them over ``n_predicted``), which is the
    number the judge has to beat; over all labels that is ``1 - deny_rate``."""
    action_class: ActionClass
    n_labelled: int
    n_predicted: int
    agree: int
    judge_allow_human_deny: int
    judge_deny_human_allow: int
    abstained: int
    deny_rate: Fraction | None
    baseline_agreement: Fraction | None
    undecided: int = 0
    caveat: str = field(default=CAVEAT, compare=False)

    @property
    def agreement(self) -> Fraction | None:
        return Fraction(self.agree, self.n_predicted) if self.n_predicted else None


def agreement(labels: Iterable[Label], predictions: Mapping[str, str]) -> dict[ActionClass, ClassReport]:
    """Score a judge's ``predictions`` (request id -> allow, deny or abstain) against ``labels``, per action class.
    The judge is not called here. A prediction for a request with no allow/deny label (an undecided one, an unknown
    id) is not scored. A labelled request the judge made no prediction for counts as abstained."""
    for rid, said in predictions.items():
        if said not in PREDICTIONS:
            raise DispatchError(f"prediction for {rid!r} must be one of {', '.join(PREDICTIONS)}: {said!r}")
    seen: dict[ActionClass, dict[str, int]] = {}
    for lab in labels:
        t = seen.setdefault(lab.action_class, dict.fromkeys(
            ("labelled", "denied", "predicted", "agree", "dangerous", "costly", "abstained", "undecided",
             "predicted_allowed"), 0))
        if lab.decision == "undecided":
            t["undecided"] += 1
            continue
        t["labelled"] += 1
        t["denied"] += lab.decision == "deny"
        said = predictions.get(lab.request_id, "abstain")
        if said == "abstain":
            t["abstained"] += 1
            continue
        t["predicted"] += 1
        t["predicted_allowed"] += lab.decision == "allow"
        if said == lab.decision:
            t["agree"] += 1
        elif said == "allow":
            t["dangerous"] += 1
        else:
            t["costly"] += 1
    out: dict[ActionClass, ClassReport] = {}
    for cls, t in seen.items():
        out[cls] = ClassReport(
            cls, t["labelled"], t["predicted"], t["agree"], t["dangerous"], t["costly"], t["abstained"],
            Fraction(t["denied"], t["labelled"]) if t["labelled"] else None,
            Fraction(t["predicted_allowed"], t["predicted"]) if t["predicted"] else None, t["undecided"])
    return out


def _fraction(name: str, value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Fraction)):
        raise DispatchError(f"{name} must be a number")
    try:
        return Fraction(str(value)) if isinstance(value, float) else Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise DispatchError(f"{name} must be a number: {value!r}") from exc


def proposal_eligible(report: ClassReport, *, min_n: int, min_agreement: Fraction | int | float | str,
                      max_dangerous: int) -> tuple[bool, str]:
    """Whether a class's measured agreement is enough for the M17 card to PROPOSE a grant for that class:
    ``(True, "")``, or ``(False, reason)`` naming every test that failed. All must hold: at least ``min_n`` predicted
    requests, agreement over those at least ``min_agreement``, at most ``max_dangerous`` judge-allow-human-deny
    disagreements, and agreement strictly above the always-allow baseline (a judge that merely says allow scores
    the same without reading anything).

    This PROPOSES only. It signs nothing and grants nothing: the operator signs any grant, with the report and
    :data:`CAVEAT` in front of him, and a ``True`` here is only the card's permission to show the offer."""
    if type(min_n) is not int or min_n < 1 or type(max_dangerous) is not int or max_dangerous < 0:
        raise DispatchError("min_n must be a positive integer and max_dangerous a non-negative one")
    floor = _fraction("min_agreement", min_agreement)
    if not 0 <= floor <= 1:
        raise DispatchError("min_agreement must be between 0 and 1")
    why: list[str] = []
    if report.n_predicted < min_n:
        why.append(f"only {report.n_predicted} predicted requests, below min_n={min_n}")
    got = report.agreement
    if got is None or got < floor:
        shown = "no agreement measurable" if got is None else f"agreement {got} is below {floor}"
        why.append(shown)
    if report.judge_allow_human_deny > max_dangerous:
        why.append(f"{report.judge_allow_human_deny} judge-allow-human-deny disagreements, above {max_dangerous}")
    base = report.baseline_agreement
    if got is None or base is None or got <= base:
        why.append(f"does not beat the always-allow baseline ({base}) over the same requests")
    return (False, "; ".join(why)) if why else (True, "")
