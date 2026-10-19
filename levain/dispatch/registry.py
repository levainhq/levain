"""levain.dispatch.registry — what each kind of dispatch configuration item holds, and the template resolver.

:mod:`levain.dispatch.control` verifies a configuration item's signature and generation; this module judges its BODY
(:func:`body_refusal`) against the configuration already active, and reads the active configuration back out in the
shapes the rest of dispatch takes: :func:`resolver` for ``JobStore.file``'s ``resolve_template`` and
:func:`active_policy` for its ``policy``.

A TEMPLATE is the input shape of :class:`levain.dispatch.contract.ResolvedTemplate`'s mapping. Its ``content_hash``
is COMPUTED here, as the SHA-256 of the template's canonical form without that field (:func:`template_content_hash`);
an item must carry the hash it was signed with and a mismatch refuses the item, so the operator signs the hash a
contract will later bind and no input chooses it. R2 is never a template: an item naming it is refused.

An R0 template's write set excludes every EXECUTION-BEARING file (DESIGN §3.2, the R0 row): a write path that is a
test, a build or CI file, a hook, agent configuration or a lockfile refuses the template whole. The intent: R0 is the
tier where the verifier IS the criterion and a result may be adopted by rule, so an executor that could rewrite what
the verifier runs, what the build runs, or what the next agent session loads would be grading or steering its own
work. :data:`EXECUTION_BEARING_FILES` and :data:`EXECUTION_BEARING_DIRS` are a FLOOR, never an allowlist: R0 stays
positive (only the template's registered paths), and a path the floor does not name is not thereby safe. Matching
casefolds both sides (the target disk is case-insensitive) and looks at every depth (``pkg/tests/x.py`` is a test).

The VERIFIERS kind maps a verifier id to its artifact hash and kind; a template naming an unknown verifier, or one
whose ``verifier_hash`` or ``verifier_kind`` disagrees with the registered verifier, is refused when it is accepted.
That check reads the ACTIVE verifiers, so acceptance order matters: the verifiers item goes first. A later verifiers
item may change a verifier a template names (replacing a verifier and its templates takes two items, verifiers
first, so refusing it would leave no order that works); the resolver then refuses that template until a templates
item agreeing with it is accepted, so a stale pairing never resolves.

An ADOPTION RULE names an R0 template whose verifier is black-box, the template's content hash (an edited template
is a new hash, so a rule never carries over to content it was not signed for) and the rule's own criteria (DESIGN
§3.2: contrast-clean is necessary, never sufficient). No adoption runs here. Stdlib only.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch.contract import ResolvedTemplate
from levain.dispatch.policy import PolicyError, ProviderPolicy, match, path_text_problem

__all__ = [
    "ADOPTION_RULE_KEYS", "EXECUTION_BEARING_DIRS", "EXECUTION_BEARING_FILES", "KINDS", "ROUTE_KEYS",
    "TEMPLATE_FORMAT", "TEMPLATE_KEYS", "VERIFIER_KEYS", "active_policy", "body_refusal", "execution_bearing",
    "resolver", "template_content_hash", "template_from_mapping",
]

KINDS = ("adoption_rules", "provider_policy", "routemap", "templates", "verifiers")
TEMPLATE_FORMAT = "levain-dispatch-template/1"
TEMPLATE_KEYS = frozenset({"id", "content_hash", "tier", "executor", "models", "k", "verifier_id", "verifier_hash",
                           "verifier_kind", "write_paths", "provider", "route", "tool_versions"})
VERIFIER_KEYS = frozenset({"artifact_hash", "kind"})
ADOPTION_RULE_KEYS = frozenset({"template_id", "template_hash", "verifier_kind", "criteria"})
ROUTE_KEYS = frozenset({"route", "provider", "upstream", "credential", "models", "body_policy", "retention_claim"})
_ENTRIES_MAX = 256
_TEXT_MAX = 1024
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# a verifier id, in the shape a template's verifier_id must have (the contract module's name rule)
_VERIFIER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}")
_VERIFIER_KINDS = ("black-box", "in-process")

# The execution-bearing floor for an R0 write set, casefolded. FILES are matched against a path's last component,
# DIRS against every component above it.
#   tests: the verifier runs them, so an executor that writes one grades itself (conftest.py runs at collection)
#   build and CI: they run on install, on build or on push, outside the contract's write grant
#   hooks: git and pre-commit run them on the operator's next commit
#   agent configuration: the next agent session loads it as instructions or settings
#   lockfiles: they choose the code every later install runs
EXECUTION_BEARING_FILES: tuple[str, ...] = (
    "test_*.py", "*_test.py", "conftest.py",
    "makefile", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    "agents.md", "claude.md",
    "*.lock", "package-lock.json", "poetry.lock", "uv.lock",
)
EXECUTION_BEARING_DIRS: tuple[str, ...] = ("tests", ".github", ".git", ".githooks", ".claude", ".codex")


class _ConfigStore(Protocol):
    """What the readers here need from :class:`levain.dispatch.control.ControlStore`: the active item of each kind,
    keyed by kind, each with its parsed ``body``; raises when the store cannot be read or verified."""
    def active(self) -> Mapping[str, Any]: ...


def execution_bearing(path: str) -> str | None:
    """The floor entry an R0 write path matches (casefolded, at any depth), or ``None``."""
    parts = path.casefold().split("/")
    for d in parts[:-1]:
        if d in EXECUTION_BEARING_DIRS:
            return d + "/**"
    for pattern in EXECUTION_BEARING_FILES:
        if match(pattern, parts[-1]):
            return pattern
    return None


def _text(value: Any, what: str) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _TEXT_MAX:
        return f"{what} must be 1 to {_TEXT_MAX} characters of text"
    problem = path_text_problem(value, what)
    return None if problem is None else problem


# --- templates -----------------------------------------------------------------------------------------------------

def template_content_hash(t: Mapping[str, Any]) -> str:
    """The SHA-256 of a template's canonical form, every field but ``content_hash`` (write paths sorted)."""
    body = {k: v for k, v in t.items() if k != "content_hash"}
    body["write_paths"] = sorted(body["write_paths"])
    return "sha256:" + hashlib.sha256(canonical_bytes({"format": TEMPLATE_FORMAT, **body})).hexdigest()


def template_from_mapping(t: Any) -> ResolvedTemplate:
    """The template a configuration entry holds, checked whole; raises :class:`DispatchError` saying why."""
    if not isinstance(t, Mapping) or set(t) != TEMPLATE_KEYS:
        raise DispatchError(f"a template is exactly {sorted(TEMPLATE_KEYS)}")
    for name in ("models", "write_paths", "tool_versions"):
        if not isinstance(t[name], list):
            raise DispatchError(f"the template's {name} is a list")
    if not all(isinstance(p, str) for p in t["write_paths"]):
        raise DispatchError("the template's write_paths are text")
    if len(t["write_paths"]) > _ENTRIES_MAX:
        raise DispatchError(f"the template lists more than {_ENTRIES_MAX} write paths")
    if len({p.casefold() for p in t["write_paths"]}) != len(t["write_paths"]):
        raise DispatchError("the template's write_paths list a path twice, or two that differ only in case")
    if not all(isinstance(p, list) and len(p) == 2 for p in t["tool_versions"]):
        raise DispatchError("the template's tool_versions are [name, version] pairs")
    try:
        template = ResolvedTemplate(
            template_id=t["id"], content_hash=t["content_hash"], tier=t["tier"], executor=t["executor"],
            models=tuple(t["models"]), k=t["k"], verifier_id=t["verifier_id"], verifier_hash=t["verifier_hash"],
            verifier_kind=t["verifier_kind"], write_paths=frozenset(t["write_paths"]), provider=t["provider"],
            route=t["route"], tool_versions=tuple(tuple(p) for p in t["tool_versions"]))
    except TypeError:
        raise DispatchError("the template holds an unhashable value") from None
    problem = template.problem()
    if problem is not None:
        raise DispatchError(f"template {str(t['id'])[:64]!r}: {problem}")
    computed = template_content_hash(t)
    if t["content_hash"] != computed:
        raise DispatchError(f"template {template.template_id}: content_hash {t['content_hash']} is not its computed "
                            f"hash {computed}")
    if template.tier == "R0":
        for p in sorted(template.write_paths):
            hit = execution_bearing(p)
            if hit is not None:
                raise DispatchError(f"R0 template {template.template_id} writes {p}, an execution-bearing file "
                                    f"({hit}); the template is refused whole")
    return template


def _verifier_mismatch(template: ResolvedTemplate, verifiers: Mapping[str, Any]) -> str | None:
    v = verifiers.get(template.verifier_id)
    if v is None:
        return f"template {template.template_id} names verifier {template.verifier_id}, which is not registered"
    if v["artifact_hash"] != template.verifier_hash:
        return (f"template {template.template_id}'s verifier_hash is not verifier {template.verifier_id}'s "
                "registered artifact hash")
    if v["kind"] != template.verifier_kind:
        return (f"template {template.template_id} calls verifier {template.verifier_id} {template.verifier_kind}; "
                f"it is registered {v['kind']}")
    return None


def _templates(body: Any, verifiers: Mapping[str, Any]) -> str | None:
    if not isinstance(body, list) or len(body) > _ENTRIES_MAX:
        return f"a templates body is a list of at most {_ENTRIES_MAX} templates"
    seen: set[str] = set()
    for t in body:
        try:
            template = template_from_mapping(t)
        except DispatchError as exc:
            return str(exc)
        if template.template_id in seen:
            return f"template {template.template_id} is listed twice"
        seen.add(template.template_id)
        problem = _verifier_mismatch(template, verifiers)
        if problem is not None:
            return problem
    return None


# --- the other kinds -----------------------------------------------------------------------------------------------

def _verifiers(body: Any) -> str | None:
    if not isinstance(body, dict) or len(body) > _ENTRIES_MAX:
        return f"a verifiers body is a mapping of at most {_ENTRIES_MAX} verifiers"
    for vid, v in body.items():
        if not isinstance(vid, str) or _VERIFIER_ID.fullmatch(vid) is None:
            return f"verifier id {str(vid)[:64]!r} must match {_VERIFIER_ID.pattern}"
        if not isinstance(v, dict) or set(v) != VERIFIER_KEYS:
            return f"verifier {vid} is exactly {sorted(VERIFIER_KEYS)}"
        if not isinstance(v["artifact_hash"], str) or _SHA256_DIGEST.fullmatch(v["artifact_hash"]) is None:
            return f"verifier {vid}'s artifact_hash must be sha256:<64 hex>"
        if v["kind"] not in _VERIFIER_KINDS:
            return f"verifier {vid}'s kind {v['kind']!r} is not one of {_VERIFIER_KINDS}"
    return None


def _provider_policy(body: Any) -> str | None:
    try:
        ProviderPolicy.from_mapping(body)
    except PolicyError as exc:
        return f"provider policy: {exc}"
    return None


def _routemap(body: Any) -> str | None:
    # keys and types only: the gateway enforces the map (SCHEMAS §1)
    if not isinstance(body, dict) or set(body) != {"routes"}:
        return 'a routemap body is exactly {"routes": [...]}'
    routes = body["routes"]
    if not isinstance(routes, list) or len(routes) > _ENTRIES_MAX:
        return f"routes is a list of at most {_ENTRIES_MAX} routes"
    seen: set[str] = set()
    for r in routes:
        if not isinstance(r, dict) or set(r) != ROUTE_KEYS:
            return f"a route is exactly {sorted(ROUTE_KEYS)}"
        for name in ("route", "provider", "upstream", "credential", "retention_claim"):
            problem = _text(r[name], f"a route's {name}")
            if problem is not None:
                return problem
        if r["route"] in seen:
            return f"route {r['route']} is listed twice"
        seen.add(r["route"])
        models = r["models"]
        if not isinstance(models, list) or not models or len(models) > _ENTRIES_MAX:
            return f"route {r['route']}: models is a non-empty list"
        requested: set[str] = set()
        for m in models:
            if not isinstance(m, dict) or set(m) != {"requested", "backend"}:
                return f"route {r['route']}: a model is exactly requested, backend"
            for name in ("requested", "backend"):
                problem = _text(m[name], f"a model's {name}")
                if problem is not None:
                    return f"route {r['route']}: {problem}"
            if m["requested"] in requested:
                return f"route {r['route']}: model {m['requested']} is listed twice"
            requested.add(m["requested"])
        bp = r["body_policy"]
        if not isinstance(bp, dict) or len(bp) > _ENTRIES_MAX:
            return f"route {r['route']}: body_policy is a mapping"
        for key, value in bp.items():
            problem = _text(key, "a body_policy key")
            if problem is not None:
                return f"route {r['route']}: {problem}"
            values = value if isinstance(value, list) else [value]
            if isinstance(value, list) and len(value) > _ENTRIES_MAX:
                return f"route {r['route']}: body_policy {key} lists too many values"
            for v in values:
                problem = _text(v, f"body_policy {key}")
                if problem is not None:
                    return f"route {r['route']}: {problem} (a value is text or a list of text)"
    return None


def _adoption_rules(body: Any, templates: Mapping[str, ResolvedTemplate]) -> str | None:
    if not isinstance(body, list) or len(body) > _ENTRIES_MAX:
        return f"an adoption_rules body is a list of at most {_ENTRIES_MAX} rules"
    for rule in body:
        if not isinstance(rule, dict) or set(rule) != ADOPTION_RULE_KEYS:
            return f"an adoption rule is exactly {sorted(ADOPTION_RULE_KEYS)}"
        tid = rule["template_id"]
        if rule["verifier_kind"] != "black-box":
            return (f"the adoption rule for {str(tid)[:64]!r} names verifier kind {rule['verifier_kind']!r}; only a "
                    "black-box-verified result may be adopted by rule (an in-process one needs a human signature)")
        template = templates.get(tid) if isinstance(tid, str) else None
        if template is None:
            return f"the adoption rule names template {str(tid)[:64]!r}, which is not an active template"
        if template.tier != "R0":
            return f"the adoption rule names template {tid}, which is {template.tier}; only an R0 template is adopted by rule"
        if template.verifier_kind != "black-box":
            return (f"the adoption rule names template {tid}, whose verifier is {template.verifier_kind}; only a "
                    "black-box verifier may adopt by rule")
        if rule["template_hash"] != template.content_hash:
            return f"the adoption rule for {tid} is bound to another template content hash than the active one"
        criteria = rule["criteria"]
        if not isinstance(criteria, list) or not criteria or len(criteria) > _ENTRIES_MAX:
            return f"the adoption rule for {tid} names its own criteria: a non-empty list"
        for c in criteria:
            problem = _text(c, "a criterion")
            if problem is not None:
                return f"the adoption rule for {tid}: {problem}"
    return None


def _active_templates(active: Mapping[str, Any]) -> dict[str, ResolvedTemplate]:
    item = active.get("templates")
    return {} if item is None else {t["id"]: template_from_mapping(t) for t in item.body}


def _active_verifiers(active: Mapping[str, Any]) -> Mapping[str, Any]:
    item = active.get("verifiers")
    return {} if item is None else item.body


def body_refusal(kind: str, body: Any, active: Mapping[str, Any]) -> str | None:
    """Why ``body`` cannot be the next ``kind`` item given the ``active`` configuration (kind -> its accepted item,
    each with a ``body``); ``None`` when it can."""
    if kind == "templates":
        return _templates(body, _active_verifiers(active))
    if kind == "verifiers":
        return _verifiers(body)
    if kind == "provider_policy":
        return _provider_policy(body)
    if kind == "routemap":
        return _routemap(body)
    if kind == "adoption_rules":
        try:
            templates = _active_templates(active)
        except DispatchError as exc:
            return f"the active templates do not load: {exc}"
        return _adoption_rules(body, templates)
    return f"kind {kind!r} is not one of {KINDS}"


# --- readers -------------------------------------------------------------------------------------------------------

def resolver(control_store: _ConfigStore) -> Callable[[str], ResolvedTemplate | None]:
    """The active templates as ``JobStore.file`` takes them (``resolve_template``): an id resolves to its template,
    or ``None`` when no active template has it. Each call reads the store afresh; a store that cannot be read or
    verified, or a template whose verifier no longer agrees with the active verifiers, RAISES (resolution then
    refuses the contract), never resolves."""
    def resolve(template_id: str) -> ResolvedTemplate | None:
        active = control_store.active()
        template = _active_templates(active).get(template_id)
        if template is None:
            return None
        problem = _verifier_mismatch(template, _active_verifiers(active))
        if problem is not None:
            raise DispatchError(problem)
        return template
    return resolve


def active_policy(control_store: _ConfigStore) -> ProviderPolicy:
    """The active provider policy; raises when the store cannot be read or verified, or no policy is accepted yet
    (no policy is never an empty one)."""
    item = control_store.active().get("provider_policy")
    if item is None:
        raise DispatchError("no provider policy has been accepted; nothing may be dispatched")
    return ProviderPolicy.from_mapping(item.body)
