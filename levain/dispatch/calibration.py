"""levain.dispatch.calibration — the label layer for a decision model (design F8, slice a).

A decision model (a "judge") may read a gated request and say allow or deny. It never authorises: nothing here, and
nothing built on it, turns a judge's word into a decision. What this module does is measure how often a candidate judge
agrees with the operator's own signed record, per action class, so that a grant PROPOSAL on the M17 card can rest on a
stated N and not on a feeling.

Labels are read from the consent queue through its public reader, :func:`levain.consent.listing.read_listing`, chosen
because it is meant to read the queue without writing it; this module writes nothing. One label per decided request:

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

A judge is REPLAYED over those labels (:func:`replay`, :func:`report`): it is shown each request as the consent listing
shows it (agent, tool, the input display, tier; never the human's decision, signer, ruling or request id) and answers
allow, deny or abstain. This slice calls no real model, on purpose. A real judge needs the gateway (wave 3), and a
consent request's content is the operator's own commands: sending it to a provider is a data-boundary decision that
belongs to that gateway, not to this layer. So :func:`replay` refuses any judge not marked ``local``, which keeps a raw
API client from being wired in by accident until a gateway-backed judge exists.

The honest limit is stated once, in :data:`CAVEAT`, and repeated by every report that prints a number. Stdlib only.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Protocol, runtime_checkable

from levain.consent.listing import ConsentEntry, read_listing
from levain.dispatch import DispatchError

__all__ = ["ActionClass", "CAVEAT", "ClassReport", "DEFAULT_SPAN", "Judge", "JudgeContext", "Label", "LabelSet",
           "PREDICTIONS", "ClassEntry", "Replay", "ReplayReport", "agreement", "deny_rate", "proposal_eligible", "read_labels",
           "replay", "report"]

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
    (see the module docstring), how many rows the reader skipped because they did not check out, and whether the set
    is ``incomplete``: the queue listing overflowed its cap (more undecided requests than it reads, or more pending,
    which it does not tell apart), so some requests were not read at all. Every report built from an incomplete set
    says so, and :func:`proposal_eligible` refuses it. ``contexts`` maps a request id to what a judge may see of it
    (:class:`JudgeContext`)."""
    labels: tuple[Label, ...] = ()
    excluded_grants: int = 0
    skipped: int = 0
    incomplete: bool = False
    contexts: Mapping[str, "JudgeContext"] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class JudgeContext:
    """The raw request as the consent listing shows it, and nothing of the answer: the agent, the tool, the input
    display and the tier. No decision, signer, ruling, grant, timestamp or request id is a field, so none can reach a
    judge."""
    agent: str
    tool_name: str
    display: str
    tier: str


def _class_of(entry: ConsentEntry) -> ActionClass:
    r = entry.request
    return (r.agent, r.tool_name, r.tier)


def _context_of(entry: ConsentEntry) -> JudgeContext:
    r = entry.request
    return JudgeContext(r.agent, r.tool_name, r.display, r.tier)


def read_labels(directory: Path | str | None = None, *, now: datetime, span: timedelta = DEFAULT_SPAN) -> LabelSet:
    """The labels in the consent queue at ``directory`` (default: the queue's own directory) decided within ``span``
    of ``now``, plus every undecided request however old, up to the listing's cap: past it the set is marked
    ``incomplete``. Read-only through :func:`read_listing`; a missing queue is an empty set."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DispatchError("read_labels needs an aware now (a naive datetime has no zone to compare stamps in)")
    listing = read_listing(directory, now=now, span=span, cap=_NO_CAP)
    labels: list[Label] = []
    contexts: dict[str, JudgeContext] = {}
    grants = 0
    for entry in (*listing.recent, *listing.undecided):
        if entry.state == "undecided":
            labels.append(Label(entry.request.id, _class_of(entry), "undecided", None, entry.decided_at))
        elif entry.state in ("allowed", "denied"):
            if entry.via == "grant":
                grants += 1
            elif entry.via in ("signature", "ruling"):
                contexts[entry.request.id] = _context_of(entry)
                labels.append(Label(entry.request.id, _class_of(entry), "allow" if entry.state == "allowed" else "deny",
                                    entry.via, entry.decided_at))
            # an allowed or denied row with any other via carries no signed judgment this layer can name: left out
    labels.sort(key=lambda lab: (lab.decided_at or "", lab.request_id))
    return LabelSet(tuple(labels), grants, listing.skipped, listing.overflow, contexts)


def _unique(labels: Iterable[Label] | LabelSet, incomplete: bool | None) -> tuple[list[Label], bool]:
    """The labels once per request id, and whether the set they came from is incomplete. The same label twice (two
    overlapping reads concatenated) counts once; two DIFFERENT labels for one request are refused, since one of them
    is not his record. A plain iterable carries no completeness of its own, so its caller must state it: a list cut
    from an incomplete :class:`LabelSet` would otherwise read as complete."""
    if isinstance(labels, LabelSet):
        incomplete = bool(incomplete) or labels.incomplete
        labels = labels.labels
    elif incomplete is None:
        raise DispatchError("a plain label list must state incomplete= (a LabelSet carries its own)")
    seen: dict[str, Label] = {}
    for lab in labels:
        prior = seen.get(lab.request_id)
        if prior is None:
            seen[lab.request_id] = lab
        elif prior != lab:
            raise DispatchError(f"request {lab.request_id!r} carries two different labels; one request is one label")
    return list(seen.values()), incomplete


# --- deny rate -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DenyRate:
    """``n`` allow and deny labels (undecided are NOT in it), ``denies`` of them denies, ``undecided`` kept apart.
    ``rate`` is exact, ``None`` with no label to divide by. ``incomplete``: the labels came from a set that did not
    read every request (see :class:`LabelSet`)."""
    n: int
    denies: int
    undecided: int
    incomplete: bool = False

    @property
    def rate(self) -> Fraction | None:
        return Fraction(self.denies, self.n) if self.n else None


def deny_rate(labels: Iterable[Label] | LabelSet, *, by_class: bool = True,
              incomplete: bool | None = None) -> dict[object, DenyRate]:
    """How often he denied, with N: per action class (``by_class``) or under the single key ``"all"``. The
    always-allow judge's score is ``1 - rate``; :func:`agreement` states that baseline per class. Each request counts
    once (see :func:`agreement`); a :class:`LabelSet` passes its ``incomplete`` flag on to every rate, and a plain
    iterable must state it with ``incomplete``."""
    unique, incomplete = _unique(labels, incomplete)
    tally: dict[object, list[int]] = {}
    for lab in unique:
        key: object = lab.action_class if by_class else "all"
        n, denies, undecided = tally.setdefault(key, [0, 0, 0])
        if lab.decision == "undecided":
            tally[key][2] = undecided + 1
        else:
            tally[key][0] = n + 1
            tally[key][1] = denies + (lab.decision == "deny")
    return {key: DenyRate(n, denies, undecided, incomplete) for key, (n, denies, undecided) in tally.items()}


# --- agreement -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassReport:
    """One action class's agreement between a judge and the operator's record.

    ``n_labelled`` allow and deny labels; ``n_predicted`` the labelled requests the judge answered (not abstained);
    ``agree`` of those; ``judge_allow_human_deny`` the dangerous direction (the judge would have let through what he
    denied), ``judge_deny_human_allow`` the costly-but-safe one. ``abstained`` labelled requests the judge passed on;
    ``deny_rate`` his rate over every labelled request of the class. ``baseline_agreement`` is what an always-allow judge
    would have scored over the SAME predicted requests (his allows among them over ``n_predicted``), which is the
    number the judge has to beat; over all labels that is ``1 - deny_rate``. ``incomplete``: the labels came from a
    set that did not read every request, so the counts may be short; :func:`proposal_eligible` refuses it."""
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
    incomplete: bool = False

    @property
    def agreement(self) -> Fraction | None:
        return Fraction(self.agree, self.n_predicted) if self.n_predicted else None


def agreement(labels: Iterable[Label] | LabelSet, predictions: Mapping[str, str], *,
              incomplete: bool | None = None) -> dict[ActionClass, ClassReport]:
    """Score a judge's ``predictions`` (request id -> allow, deny or abstain) against ``labels``, per action class.
    The judge is not called here. A prediction for a request with no allow/deny label (an undecided one, an unknown
    id) is not scored. A labelled request the judge made no prediction for counts as abstained.

    Each request counts ONCE: the same label repeated (overlapping reads concatenated) is counted once, and two
    different labels for one request id are refused (:class:`DispatchError`), so N is the number of his decisions,
    never of rows. A :class:`LabelSet` passes its ``incomplete`` flag to every report; for a plain iterable the
    caller must state it with ``incomplete``."""
    for rid, said in predictions.items():
        if said not in PREDICTIONS:
            raise DispatchError(f"prediction for {rid!r} must be one of {', '.join(PREDICTIONS)}: {said!r}")
    unique, incomplete = _unique(labels, incomplete)
    seen: dict[ActionClass, dict[str, int]] = {}
    for lab in unique:
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
            Fraction(t["predicted_allowed"], t["predicted"]) if t["predicted"] else None, t["undecided"],
            incomplete=incomplete)
    return out


def _fraction(name: str, value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Fraction)):
        raise DispatchError(f"{name} must be a number")
    try:
        return Fraction(str(value)) if isinstance(value, float) else Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise DispatchError(f"{name} must be a number: {value!r}") from exc


def proposal_eligible(report: ClassReport, *, min_n: int, min_agreement: Fraction | int | float | str,
                      min_coverage: Fraction | int | float | str, max_dangerous: int) -> tuple[bool, str]:
    """Whether a class's measured agreement is enough for the M17 card to PROPOSE a grant for that class:
    ``(True, "")``, or ``(False, reason)`` naming every test that failed. All must hold: at least ``min_n`` predicted
    requests, agreement over those at least ``min_agreement``, coverage (predicted over labelled: how much of his
    record the judge was willing to answer) at least ``min_coverage``, a COMPLETE label set (not ``incomplete``), at
    most ``max_dangerous``
    judge-allow-human-deny disagreements, and agreement strictly above the always-allow baseline (a judge that merely
    says allow scores the same without reading anything). The coverage floor is required: without it a judge that
    abstains on every hard call can pass on the easy ones.

    This PROPOSES only. It signs nothing and grants nothing: the operator signs any grant, with the report and
    :data:`CAVEAT` in front of him, and a ``True`` here is only the card's permission to show the offer."""
    if type(min_n) is not int or min_n < 1 or type(max_dangerous) is not int or max_dangerous < 0:
        raise DispatchError("min_n must be a positive integer and max_dangerous a non-negative one")
    floor = _fraction("min_agreement", min_agreement)
    if not 0 <= floor <= 1:
        raise DispatchError("min_agreement must be between 0 and 1")
    coverage_floor = _fraction("min_coverage", min_coverage)
    if not 0 <= coverage_floor <= 1:
        raise DispatchError("min_coverage must be between 0 and 1")
    why: list[str] = []
    if report.incomplete:
        why.append("the label set is incomplete (the queue listing overflowed its cap), so its counts may be short")
    if report.n_predicted < min_n:
        why.append(f"only {report.n_predicted} predicted requests, below min_n={min_n}")
    coverage = Fraction(report.n_predicted, report.n_labelled) if report.n_labelled else None
    if coverage is None or coverage < coverage_floor:
        why.append(f"the judge answered {report.n_predicted} of {report.n_labelled} labelled requests, coverage "
                   f"below {coverage_floor}")
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


# --- the judge replay ------------------------------------------------------------------------------------------

@runtime_checkable
class Judge(Protocol):
    """A candidate decision model. ``local`` must be ``True`` for :func:`replay` to run it (see the module
    docstring). ``predict`` gets the request as a :class:`JudgeContext` and returns ``"allow"``, ``"deny"`` or
    ``"abstain"``. A judge never authorises; its word is scored, never acted on."""
    local: bool

    def predict(self, context: JudgeContext) -> str: ...


@dataclass(frozen=True)
class Replay:
    """What :func:`replay` got: ``predictions`` (request id -> allow, deny or abstain) for every replayed request,
    ``raised`` (request id -> the exception's class name) for the ones where the judge raised or answered something
    outside the three words; each of those is recorded in ``predictions`` as ``abstain``. ``replayed`` the request ids
    shown to the judge, in order."""
    predictions: Mapping[str, str]
    raised: Mapping[str, str]
    replayed: tuple[str, ...]


def replay(labels: LabelSet, judge: Judge, *, limit: int | None = None) -> Replay:
    """Show ``judge`` each allow/deny request in ``labels`` (oldest decision first; at most ``limit``) and record its
    answer. Undecided requests are not shown: there is no human judgment to score. A judge exception is an
    ``abstain`` recorded with its class name, never a crash and never a guess. ``labels`` must be a
    :class:`LabelSet` from :func:`read_labels`: a bare list carries no request content to show."""
    if getattr(judge, "local", None) is not True or not callable(getattr(judge, "predict", None)):
        raise DispatchError("replay runs only a judge marked local (judge.local is True) with a predict method: no real "
                            "model is called until a gateway-backed judge exists")
    if not isinstance(labels, LabelSet):
        raise DispatchError("replay needs the LabelSet read_labels returns (it carries the request contexts)")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise DispatchError("limit must be a non-negative integer")
    unique, _ = _unique(labels, None)
    todo = [lab for lab in unique if lab.decision != "undecided"]
    if limit is not None:
        todo = todo[:limit]
    predictions: dict[str, str] = {}
    raised: dict[str, str] = {}
    for lab in todo:
        context = labels.contexts.get(lab.request_id)
        if context is None:
            raise DispatchError(f"request {lab.request_id!r} has no context to show the judge")
        try:
            said = judge.predict(context)
        except Exception as exc:                                # noqa: BLE001 - any judge failure is an abstain
            predictions[lab.request_id], raised[lab.request_id] = "abstain", type(exc).__name__
            continue
        if said not in PREDICTIONS:
            predictions[lab.request_id], raised[lab.request_id] = "abstain", "InvalidPrediction"
        else:
            predictions[lab.request_id] = said
    return Replay(predictions, raised, tuple(lab.request_id for lab in todo))


@dataclass(frozen=True)
class ClassEntry:
    """One action class in a :class:`ReplayReport`: the agreement report, his deny rate, and the proposal verdict
    (``eligible`` and ``why_not``; the card may show an offer only on ``True``)."""
    report: ClassReport
    deny_rate: DenyRate
    eligible: bool
    why_not: str


@dataclass(frozen=True)
class ReplayReport:
    """A judge's replay as plain data: per action class entries, how many requests the judge was shown and how many
    raised, the caveat, and ``incomplete`` (the queue listing overflowed, or ``limit`` cut the replay short: either
    way the counts are a sample and no proposal rests on them)."""
    classes: Mapping[ActionClass, ClassEntry]
    replayed: int
    raised: int
    incomplete: bool
    caveat: str = CAVEAT


def report(labels: LabelSet, judge: Judge, *, min_n: int, min_agreement: Fraction | int | float | str,
           min_coverage: Fraction | int | float | str, max_dangerous: int, limit: int | None = None) -> ReplayReport:
    """:func:`replay` + :func:`agreement` + :func:`deny_rate` + :data:`CAVEAT` + :func:`proposal_eligible`, per action
    class. The thresholds are required: this layer owns no default for what is enough. A ``limit`` that cut the
    replay marks the report incomplete, since the unreplayed requests would otherwise read as abstentions."""
    run = replay(labels, judge, limit=limit)
    shown = set(run.replayed)
    sample = [lab for lab in labels.labels if lab.request_id in shown]
    cut = limit is not None and len(run.replayed) < sum(1 for lab in labels.labels if lab.decision != "undecided")
    incomplete = labels.incomplete or cut
    agreed = agreement(sample, run.predictions, incomplete=incomplete)
    rates = deny_rate(sample, incomplete=incomplete)
    classes: dict[ActionClass, ClassEntry] = {}
    for cls, rep_ in agreed.items():
        ok, why = proposal_eligible(rep_, min_n=min_n, min_agreement=min_agreement, min_coverage=min_coverage,
                                    max_dangerous=max_dangerous)
        classes[cls] = ClassEntry(rep_, rates[cls], ok, why)
    return ReplayReport(classes, len(run.replayed), len(run.raised), incomplete)
