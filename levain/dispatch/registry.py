"""levain.dispatch.registry — what each kind of dispatch configuration item holds, and the template resolver.

:mod:`levain.dispatch.control` verifies a configuration item's signature and generation; this module judges its BODY
(:func:`body_refusal`) against the configuration already active, and reads the active configuration back out in the
shapes the rest of dispatch takes: :func:`resolver` for ``JobStore.file``'s ``resolve_template`` and
:func:`active_policy` for its ``policy``.

A TEMPLATE is the input shape of :class:`levain.dispatch.contract.ResolvedTemplate`'s mapping. Its ``content_hash``
is COMPUTED here, as the SHA-256 of the template's canonical form without that field (:func:`template_content_hash`);
an item must carry the hash it was signed with and a mismatch refuses the item, so the operator signs the hash a
contract will later bind and no input chooses it. R2 is never a template: an item naming it is refused.

An R0 template writes only CODE UNDER TEST and its docs (DESIGN §3.2, the R0 row): every write path must be
POSITIVELY eligible, a source or doc file by extension (:data:`R0_SOURCE_EXTENSIONS`), and must not be
EXECUTION-BEARING (:data:`EXECUTION_BEARING_FILES`, :data:`EXECUTION_BEARING_DIRS`), the deny list winning over the
allowlist (:func:`r0_write_refusal`). One path that fails refuses the template whole. The intent: R0 is the tier where
the verifier IS the criterion and a result may be adopted by rule, so an executor that could rewrite what the
verifier runs, what the build or an interpreter runs, or what the next agent session loads would be grading or
steering its own work. Allowing by kind means a file type nobody thought of is refused, not admitted; the deny list
then removes the eligible-looking files that execute (a test module, ``setup.py``, ``sitecustomize.py``, an agent's
instructions in Markdown). An ordinary module, ``__init__.py`` included, is what R0 exists to write, so it is
eligible. Matching casefolds both sides (the target disk is case-insensitive) and looks at every depth
(``pkg/tests/x.py`` is a test).

The VERIFIER kind is the S1 contract's (§3.5 VERIFIER IDENTITY): one signed item serves dispatch and CONTROL, so its
envelope kind is ``verifier`` and its body ``levain-verifier-config/1``, ``{"format", "verifiers": [entry, ...]}``.
An entry is exactly :data:`VERIFIER_KEYS`: the contract's seven fields plus the ``steps`` and ``driver_input`` their
digests are over (dispatch needs the bytes, to mint ``verifier_steps`` and to stage ``driver_input.json``; CONTROL
reads only the digests). Every digest is CHECKED at accept: ``steps_digest`` and ``driver_input_digest`` over the
canonical bytes they name, ``driver_template_digest`` over the bytes of the template this levain ships
(:func:`driver_template_digest`), and ``verifier_artifact_digest`` over what runs (:func:`verifier_artifact_digest`).
A template's ``verifier_hash`` must equal its verifier's ``verifier_artifact_digest`` and its ``verifier_kind`` the
``declared_kind``; a template naming an unknown verifier, or disagreeing, is refused when it is accepted. That check
reads the ACTIVE verifier item, so acceptance order matters: the verifier item goes first. A later verifier item may
change a verifier a template names (replacing a verifier and its templates takes two items, verifier first, so
refusing it would leave no order that works); the resolver then refuses that template until a templates item
agreeing with it is accepted, so a stale pairing never resolves. An in-process entry is accepted and shown as
forgeable by the code under test; only a black-box one may adopt by rule. A ticket's verifier fields are DERIVED from
the active entry (:func:`verifier_ticket`), never taken from a planner or a job.

Levain 0.11 signed a ``verifiers`` kind (a mapping of id to artifact hash and kind). A stored item of that kind still
chains and verifies, so the store stays readable, but it is no longer READ: nothing resolves against it, a new one is
refused, and ``levain dispatch config show`` and ``levain doctor`` say so with the way out
(:func:`legacy_verifiers_problem`). A signed item is never rewritten in place.

An ADOPTION RULE names the repository it lands in (an absolute path in contract.py's normal form), an R0 template
whose verifier is black-box, the template's content hash (an edited template is a new hash, so a rule never carries
over to content it was not signed for) and the rule's own criteria (DESIGN §3.2: contrast-clean is necessary, never
sufficient). The criteria are a closed, typed vocabulary (F11 §5, :func:`parse_criteria`): free text was checked by
nothing when a job landed, so a rule holding any is refused when it is accepted and raises when it is read. A rule
is judged when it is accepted AND again on every read (:func:`active_adoption_rules`), against the templates and
verifiers active THEN: a later templates or verifiers
item can leave a rule pointing at content or a verifier it was not signed for, and such a rule raises rather than
reads. The intent is that this reader is the only way to the rules: the store's public reader is meant to leave
them out. No adoption runs here.

Templates load per id: a stored template the current rules refuse raises only when that id is resolved, naming it.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from levain.contracts.canon import canonical_bytes, parse
from levain.dispatch import DispatchError
from levain.dispatch import routemap_rules
from levain.dispatch.contract import ResolvedTemplate
from levain.dispatch.policy import PolicyError, ProviderPolicy, match, path_text_problem
from levain.dispatch.verifier_report import _steps_problem, artifact_digest
from levain.slot.routemaps import DRIVER_INPUT_MAX as MAX_DRIVER_INPUT_BYTES
from levain.slot.routemaps import VERIFIER_ITEM_MAX_BYTES, VERIFIER_SIG_MAX

__all__ = [
    "ADOPTION_RULE_KEYS", "CRITERION_BOUNDS", "CRITERION_INT_MAX", "CRITERION_KEYS", "EXECUTION_BEARING_DIRS",
    "EXECUTION_BEARING_FILES", "KINDS", "R0_SOURCE_EXTENSIONS", "ROUTE_KEYS", "TEMPLATE_FORMAT", "TEMPLATE_KEYS",
    "LEGACY_KINDS", "MAX_DRIVER_INPUT_BYTES", "MAX_STEPS_BYTES", "RESIGN_RULES", "RESIGN_VERIFIERS",
    "VERIFIER_FORMAT", "VERIFIER_KEYS", "AdoptionRule", "active_adoption_rules", "adoption_rules_at",
    "active_policy", "active_verifier_entries", "body_refusal", "driver_template_digest", "driver_template_path",
    "execution_bearing", "legacy_rule_problem", "legacy_verifiers_problem", "parse_criteria", "r0_write_refusal",
    "resolver", "template_content_hash", "template_from_mapping", "verifier_artifact_digest", "verifier_item_refusal",
    "verifier_ticket",
]

KINDS = ("adoption_rules", "provider_policy", "routemap", "templates", "verifier")
# kinds a stored item may carry that are no longer accepted or read (module docstring)
LEGACY_KINDS = ("verifiers",)
TEMPLATE_FORMAT = "levain-dispatch-template/1"
TEMPLATE_KEYS = frozenset({"id", "content_hash", "tier", "executor", "models", "k", "verifier_id", "verifier_hash",
                           "verifier_kind", "write_paths", "provider", "route", "tool_versions"})
VERIFIER_FORMAT = "levain-verifier-config/1"
VERIFIER_KEYS = frozenset({"verifier_id", "verifier_artifact_digest", "declared_kind", "driver_template",
                           "driver_template_digest", "steps", "steps_digest", "driver_input", "driver_input_digest"})
# the canonical bytes of an entry's steps; the step count and each step's bounds are S1's runtime grammar's
MAX_STEPS_BYTES = 32 * 1024
ADOPTION_RULE_KEYS = frozenset({"repo", "template_id", "template_hash", "verifier_kind", "criteria"})
# F11 §5: the closed vocabulary of a rule's criteria; each criterion is an object with exactly one of these keys
CRITERION_KEYS = ("onto", "max_changed_lines", "max_bytes", "max_files", "paths", "no_new_files", "max_age_hours")
# a rule names onto AND at least one of these: a blast-radius bound the operator chose
CRITERION_BOUNDS = ("max_changed_lines", "max_bytes", "max_files", "paths")
# the upper bound of each integer criterion (each is also at least 1)
CRITERION_INT_MAX = {"max_changed_lines": 1_000_000, "max_bytes": 1 << 30, "max_files": 10_000,
                     "max_age_hours": 24 * 366}
_BRANCH_MAX = 255
ROUTE_KEYS = frozenset({"route", "provider", "upstream", "credential", "models", "body_policy", "retention_claim"})
_ENTRIES_MAX = routemap_rules.ENTRIES_MAX
_TEXT_MAX = routemap_rules.TEXT_MAX
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# a verifier id and a driver template name, as the S1 contract writes them; a template's verifier_id has the same shape
_VERIFIER_ID = re.compile(r"[a-z0-9-]{1,48}")
_DRIVER_TEMPLATE = re.compile(r"[a-z0-9_]{1,48}")
_VERIFIER_KINDS = ("black-box", "in-process")

# The extensions an R0 write path may have, casefolded: source a verifier tests, and the docs and styles beside it.
# A script (.sh) is not here: running it is its purpose. Neither are data and configuration formats (.json, .toml,
# .yaml, .ini): a manifest or a tool's settings decide what runs, and no extension tells one from plain data.
R0_SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".php", ".lua",
    ".md", ".rst", ".txt", ".css", ".scss", ".html", ".svg",
)

# The execution-bearing deny list, casefolded; it wins over the allowlist. FILES are matched against a path's last
# component, DIRS against every component above it.
#   tests: the verifier runs them, so an executor that writes one grades itself (conftest.py runs at collection)
#   build and CI: they run on install, on build or on push, outside the contract's write grant
#   hooks and git attributes: git and pre-commit run them, or the filters they name, on the operator's next commit
#   interpreter start-up and tool rc files: Python, the shell or a tool loads them on its next start
#   agent and editor configuration: the next agent session or the editor loads it as instructions or tasks
#   lockfiles: they choose the code every later install runs
EXECUTION_BEARING_FILES: tuple[str, ...] = (
    "test_*.*", "test.py", "tests.py", "*.test.*", "*.spec.*", "*_test.*", "*_spec.*", "conftest.py",
    "makefile", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", "build.rs", "*.config.*",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml", ".gitattributes", ".gitmodules",
    "sitecustomize.py", "usercustomize.py", "*.pth", ".envrc", ".*rc", ".*rc.*",
    "agents.md", "claude.md", "claude.local.md", "gemini.md", ".mcp.json",
    "*.lock", "package-lock.json", "poetry.lock", "uv.lock", "requirements*.txt", "constraints*.txt",
    # JS tooling a test or build run loads without the word "config" in its name
    "*.setup.*", "setup*.*", "*.conf.*", "gulpfile.*", "gruntfile.*",
    # test classes named in CamelCase (JUnit, NUnit, ScalaTest, PHPUnit, XCTest): scoped to those languages'
    # extensions, so a Python module like latest.py stays source
    *(f"{shape}.{ext}" for ext in ("java", "kt", "scala", "cs", "php", "swift", "m")
      for shape in ("*test", "*tests", "*spec", "test*", "*testcase")),
    # JVM integration tests (failsafe's FooIT, FooITCase); fail-safe false positives like Commit.java are accepted
    *(f"{shape}.{ext}" for ext in ("java", "kt") for shape in ("*it", "*itcase")),
)
EXECUTION_BEARING_DIRS: tuple[str, ...] = (
    "tests", "test", "testing", "__tests__", "spec", "e2e",
    ".github", ".circleci", ".git", ".githooks", ".husky",
    ".claude", ".codex", ".vscode", ".idea",
    "__pycache__",
)


class _ConfigStore(Protocol):
    """What the readers here need from :class:`levain.dispatch.control.ControlStore`: the active item of each kind,
    keyed by kind, each with its parsed ``body`` (``active`` without the adoption rules, ``_verified_items`` with
    them, for :func:`active_adoption_rules` alone); each raises when the store cannot be read or verified."""
    def active(self) -> Mapping[str, Any]: ...

    def _verified_items(self) -> Mapping[str, Any]: ...


def execution_bearing(path: str) -> str | None:
    """The deny-list entry an R0 write path matches (casefolded, at any depth), or ``None``."""
    parts = path.casefold().split("/")
    for d in parts[:-1]:
        if d in EXECUTION_BEARING_DIRS:
            return d + "/**"
    for pattern in EXECUTION_BEARING_FILES:
        if match(pattern, parts[-1]):
            return pattern
    return None


def r0_write_refusal(path: str) -> str | None:
    """Why an R0 template may not write ``path``: execution-bearing (checked first, so it wins), or not an eligible
    source or doc file; ``None`` when it may. Only the path's form is judged here; the contract path rule still
    applies to it."""
    hit = execution_bearing(path)
    if hit is not None:
        return f"{path} is an execution-bearing file ({hit})"
    ext = posixpath.splitext(path.rsplit("/", 1)[-1])[1].casefold()
    if ext not in R0_SOURCE_EXTENSIONS:
        return f"{path} is not R0-eligible source or docs (extension {ext or 'none'!r})"
    return None


_text = routemap_rules.text_problem


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
    if _VERIFIER_ID.fullmatch(template.verifier_id) is None:
        raise DispatchError(f"template {template.template_id}: verifier_id {template.verifier_id[:64]!r} must match "
                            f"{_VERIFIER_ID.pattern}")
    computed = template_content_hash(t)
    if t["content_hash"] != computed:
        raise DispatchError(f"template {template.template_id}: content_hash {t['content_hash']} is not its computed "
                            f"hash {computed}")
    if template.tier == "R0":
        for p in sorted(template.write_paths):
            problem = r0_write_refusal(p)
            if problem is not None:
                raise DispatchError(f"R0 template {template.template_id}: {problem}; the template is refused whole")
    return template


def _verifier_mismatch(template: ResolvedTemplate, verifiers: Mapping[str, Any]) -> str | None:
    v = verifiers.get(template.verifier_id)
    if v is None:
        return f"template {template.template_id} names verifier {template.verifier_id}, which is not registered"
    if v["verifier_artifact_digest"] != template.verifier_hash:
        return (f"template {template.template_id}'s verifier_hash is not verifier {template.verifier_id}'s "
                "verifier_artifact_digest")
    if v["declared_kind"] != template.verifier_kind:
        return (f"template {template.template_id} calls verifier {template.verifier_id} {template.verifier_kind}; "
                f"it is declared {v['declared_kind']}")
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


# --- verifiers -----------------------------------------------------------------------------------------------------

def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def driver_template_path(name: str) -> Path | None:
    """The driver template ``name`` this levain package ships (``levain/verifier_drivers/<name>.py``), or ``None``
    when ``name`` is not a template name or no such regular file ships (a symlink is not one)."""
    if not isinstance(name, str) or _DRIVER_TEMPLATE.fullmatch(name) is None:
        return None
    from levain import verifier_drivers

    path = Path(verifier_drivers.__file__).parent / f"{name}.py"
    if name.startswith("_") or path.is_symlink() or not path.is_file():
        return None
    return path


def driver_template_digest(name: str) -> str | None:
    """The SHA-256 of the shipped template ``name``'s bytes (what an entry's ``driver_template_digest`` pins), or
    ``None`` when no such template ships."""
    path = driver_template_path(name)
    return None if path is None else _sha256(path.read_bytes())


# the verifier ARTIFACT's digest, exactly what runs; one formula, shared with the minted run's check
# (levain.dispatch.verifier_report.artifact_digest, which imports nothing from this module)
verifier_artifact_digest = artifact_digest


# the shipped templates whose driver_input is {"steps": [one expectation per ticket step]}: the count and
# the driver's own reading of each expectation are checked at accept, so a mismatch is never found first by a run.
# A new template of that shape is added here, or its count goes unchecked.
_STEP_INDEXED_INPUT = frozenset({"output_compare"})


def _verifier_entry_problem(e: Any) -> str | None:
    if not isinstance(e, dict) or set(e) != VERIFIER_KEYS:
        return f"a verifier entry is exactly {sorted(VERIFIER_KEYS)}"
    vid = e["verifier_id"]
    if not isinstance(vid, str) or _VERIFIER_ID.fullmatch(vid) is None:
        return f"verifier id {str(vid)[:64]!r} must match {_VERIFIER_ID.pattern}"
    for name in ("verifier_artifact_digest", "driver_template_digest", "steps_digest", "driver_input_digest"):
        if not isinstance(e[name], str) or _SHA256_DIGEST.fullmatch(e[name]) is None:
            return f"verifier {vid}'s {name} must be sha256:<64 hex>"
    if e["declared_kind"] not in _VERIFIER_KINDS:
        return f"verifier {vid}'s declared_kind {str(e['declared_kind'])[:64]!r} is not one of {_VERIFIER_KINDS}"
    tname = e["driver_template"]
    if not isinstance(tname, str) or _DRIVER_TEMPLATE.fullmatch(tname) is None:
        return f"verifier {vid}'s driver_template {str(tname)[:64]!r} must match {_DRIVER_TEMPLATE.pattern}"
    shipped = driver_template_digest(tname)
    if shipped is None:
        return f"verifier {vid}'s driver_template {tname} is not a template this levain ships"
    if e["driver_template_digest"] != shipped:
        return (f"verifier {vid}'s driver_template_digest is not the digest of the {tname} template this levain "
                f"ships ({shipped})")
    problem = _steps_problem(e["steps"])
    if problem is not None:
        return f"verifier {vid}: {problem}"
    steps = canonical_bytes(e["steps"])
    if len(steps) > MAX_STEPS_BYTES:
        return f"verifier {vid}'s steps are over {MAX_STEPS_BYTES} bytes as canonical JSON"
    if e["steps_digest"] != _sha256(steps):
        return f"verifier {vid}'s steps_digest is not the digest of its steps"
    if not isinstance(e["driver_input"], dict):
        return f"verifier {vid}'s driver_input is an object"
    if tname in _STEP_INDEXED_INPUT:
        pinned = e["driver_input"].get("steps")
        if not isinstance(pinned, list) or len(pinned) != len(e["steps"]):
            return (f"verifier {vid}'s driver_input pins {len(pinned) if isinstance(pinned, list) else 'no'} "
                    f"step expectation(s) for {len(e['steps'])} step(s); the {tname} template reads one per step")
    driver_input = canonical_bytes(e["driver_input"])
    if len(driver_input) > MAX_DRIVER_INPUT_BYTES:
        return f"verifier {vid}'s driver_input is over {MAX_DRIVER_INPUT_BYTES} bytes as canonical JSON"
    if tname in _STEP_INDEXED_INPUT:
        # the driver's own reader, so what accept takes is exactly what a run reads (one rule, never a copy)
        from levain.verifier_drivers import output_compare

        try:
            output_compare.expectations(driver_input)
        except output_compare.Infrastructure as exc:
            return f"verifier {vid}'s driver_input would be refused by the {tname} template on every run: {exc}"
    if e["driver_input_digest"] != _sha256(driver_input):
        return f"verifier {vid}'s driver_input_digest is not the digest of its driver_input"
    if e["verifier_artifact_digest"] != verifier_artifact_digest(e):
        return (f"verifier {vid}'s verifier_artifact_digest is not the digest of what runs "
                f"({verifier_artifact_digest(e)})")
    return None


def _verifier(body: Any) -> str | None:
    if not isinstance(body, dict) or set(body) != {"format", "verifiers"}:
        return 'a verifier body is exactly {"format", "verifiers"}'
    if body["format"] != VERIFIER_FORMAT:
        return f"a verifier body's format is {VERIFIER_FORMAT!r}"
    entries = body["verifiers"]
    if not isinstance(entries, list) or len(entries) > _ENTRIES_MAX:
        return f"verifiers is a list of at most {_ENTRIES_MAX} entries"
    seen: set[str] = set()
    for e in entries:
        problem = _verifier_entry_problem(e)
        if problem is not None:
            return problem
        if e["verifier_id"] in seen:
            return f"verifier {e['verifier_id']} is listed twice"
        seen.add(e["verifier_id"])
    return None


RESIGN_VERIFIERS = ("sign a new verifier item (format levain-verifier-config/1, each entry naming a driver template "
                    "this levain ships): `levain dispatch config sign verifier <body.json>`")


def legacy_verifiers_problem(items: Mapping[str, Any]) -> str | None:
    """The line for a stored Levain 0.11 ``verifiers`` item among ``items`` (kind -> its latest item), or ``None``
    when there is none. Such an item is not read, never migrated: its ids and hashes say nothing of what runs."""
    item = items.get("verifiers")
    if item is None:
        return None
    return (f"the verifiers item at generation {item.generation} is Levain 0.11's shape and is no longer read; "
            f"templates resolve against the active verifier item only; {RESIGN_VERIFIERS}")


# --- the other kinds -----------------------------------------------------------------------------------------------


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
    problem = routemap_rules.routes_count_problem(routes)
    if problem is not None:
        return problem
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
        problem = routemap_rules.models_count_problem(r["route"], r["models"])
        if problem is not None:
            return problem
        requested: set[str] = set()
        models = r["models"]
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
        problem = routemap_rules.body_policy_problem(r["route"], r["body_policy"])
        if problem is not None:
            return problem
    return None


def _rule_repo_problem(repo: Any) -> str | None:
    # contract.py's form for a repo: an absolute path in its normal form (a rule is matched to a contract's repo by
    # equality, so it must be written the way contracts carry it)
    if not isinstance(repo, str) or not repo or "\x00" in repo or len(repo) > _TEXT_MAX:
        return "an adoption rule's repo is an absolute path"
    if not repo.startswith("/") or os.path.normpath(repo) != repo:
        return f"an adoption rule's repo {repo[:120]!r} must be an absolute path in its normal form"
    return None


def _positive_int(value: Any, name: str) -> str | None:
    high = CRITERION_INT_MAX[name]
    if type(value) is not int or not 1 <= value <= high:
        return f"criterion {name} is an integer from 1 to {high}"
    return None


def parse_criteria(criteria: Any, write_paths: frozenset[str]) -> tuple[tuple[str, Any], ...]:
    """A rule's criteria (F11 §5) as ``(key, value)`` pairs in their written order, every value checked; raises
    :class:`DispatchError` saying why not. ``write_paths`` is the template's write set: a ``paths`` entry is one of
    its paths (compared without case) or a directory, written ``dir/``, above at least one of them."""
    if not isinstance(criteria, list) or not criteria or len(criteria) > _ENTRIES_MAX:
        raise DispatchError("criteria is a non-empty list of typed criteria")
    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    folded = {p.casefold() for p in write_paths}
    for c in criteria:
        if not isinstance(c, dict) or len(c) != 1:
            raise DispatchError(f"a criterion is an object with exactly one of {list(CRITERION_KEYS)} (free text is "
                                "checked by nothing, so it is refused)")
        (key, value), = c.items()
        if key not in CRITERION_KEYS:
            raise DispatchError(f"criterion {str(key)[:64]!r} is not one of {list(CRITERION_KEYS)}")
        if key in seen:
            raise DispatchError(f"criterion {key} is named twice")
        seen.add(key)
        if key == "onto":
            if not isinstance(value, list) or not value or len(value) > _ENTRIES_MAX:
                raise DispatchError("criterion onto is a non-empty list of branch names")
            for b in value:
                if not isinstance(b, str) or not b or len(b) > _BRANCH_MAX or b.startswith("refs/") or \
                        b.casefold().startswith("levain/") or path_text_problem(b, "a branch") is not None:
                    raise DispatchError(f"criterion onto names {str(b)[:64]!r}, which is not a bare branch outside "
                                        "levain/")
            if len(set(value)) != len(value):
                raise DispatchError("criterion onto names a branch twice")
            value = tuple(value)
        elif key == "paths":
            if not isinstance(value, list) or not value or len(value) > _ENTRIES_MAX:
                raise DispatchError("criterion paths is a non-empty list")
            for p in value:
                if not isinstance(p, str) or not p:
                    raise DispatchError("criterion paths lists text paths")
                if p.endswith("/"):
                    d = p.casefold()
                    if not any(f.startswith(d) for f in folded):
                        raise DispatchError(f"criterion paths names {p[:80]!r}, above none of the template's write "
                                            "paths")
                elif p.casefold() not in folded:
                    raise DispatchError(f"criterion paths names {p[:80]!r}, which the template does not write")
            value = tuple(value)
        elif key == "no_new_files":
            if value is not True:
                raise DispatchError("criterion no_new_files is true")
        else:
            problem = _positive_int(value, key)
            if problem is not None:
                raise DispatchError(problem)
        out.append((key, value))
    if "onto" not in seen:
        raise DispatchError("a rule's criteria name onto (the branches it may land on)")
    if not seen & set(CRITERION_BOUNDS):
        raise DispatchError(f"a rule's criteria name at least one bound of {list(CRITERION_BOUNDS)}")
    return tuple(out)


RESIGN_RULES = ("sign a new adoption_rules item naming repo and typed criteria (onto plus at least one bound, such as "
                "max_files): `levain dispatch config sign adoption_rules <body.json>`")


def legacy_rule_problem(rule: Any) -> str | None:
    """The line for a rule whose criteria hold free text, the only shape Levain 0.11.10 accepted, or ``None``.
    Such a rule is refused, never migrated: a text criterion cannot become a typed one without inventing what it
    checks. It adopted nothing in 0.11.10 (no adoption ran) and adopts nothing now."""
    if not isinstance(rule, dict):
        return None
    criteria = rule.get("criteria")
    if not (isinstance(criteria, list) and any(isinstance(c, str) for c in criteria)):
        return None
    tid = rule.get("template_id")
    return (f"the adoption rule for {str(tid)[:64]!r} has criteria as free text, the shape Levain 0.11.10 accepted; "
            f"free text is checked by nothing when a job lands, so the rule adopts nothing; {RESIGN_RULES}")


def _adoption_rules(body: Any, active: Mapping[str, Any]) -> str | None:
    if not isinstance(body, list) or len(body) > _ENTRIES_MAX:
        return f"an adoption_rules body is a list of at most {_ENTRIES_MAX} rules"
    for rule in body:
        problem = legacy_rule_problem(rule)
        if problem is not None:
            return problem
        if not isinstance(rule, dict) or set(rule) != ADOPTION_RULE_KEYS:
            return f"an adoption rule is exactly {sorted(ADOPTION_RULE_KEYS)}"
        tid = rule["template_id"]
        if rule["verifier_kind"] != "black-box":
            return (f"the adoption rule for {str(tid)[:64]!r} names verifier kind {rule['verifier_kind']!r}; only a "
                    "black-box-verified result may be adopted by rule (an in-process one needs a human signature)")
        try:
            template = _active_template(active, tid) if isinstance(tid, str) else None
        except DispatchError as exc:
            return f"the adoption rule for {tid}: {exc}"
        if template is None:
            return f"the adoption rule names template {str(tid)[:64]!r}, which is not an active template"
        problem = _verifier_mismatch(template, _active_verifiers(active))
        if problem is not None:
            return f"the adoption rule for {tid}: {problem}"
        if template.tier != "R0":
            return (f"the adoption rule names template {tid}, which is {template.tier}; only an R0 template is "
                    "adopted by rule")
        if template.verifier_kind != "black-box":
            return (f"the adoption rule names template {tid}, whose verifier is {template.verifier_kind}; only a "
                    "black-box verifier may adopt by rule")
        if rule["template_hash"] != template.content_hash:
            return f"the adoption rule for {tid} is bound to another template content hash than the active one"
        problem = _rule_repo_problem(rule["repo"])
        if problem is not None:
            return f"the adoption rule for {tid}: {problem}"
        try:
            parse_criteria(rule["criteria"], template.write_paths)
        except DispatchError as exc:
            return f"the adoption rule for {tid}: {exc}"
    return None


def _active_template(active: Mapping[str, Any], template_id: str) -> ResolvedTemplate | None:
    """The active template ``template_id``, loaded alone (another template that no longer loads does not touch
    it), or ``None`` when no active template has that id; raises naming it when it does not load."""
    item = active.get("templates")
    entry = None if item is None else next(
        (t for t in item.body if isinstance(t, Mapping) and t.get("id") == template_id), None)
    if entry is None:
        return None
    try:
        return template_from_mapping(entry)
    except DispatchError as exc:
        raise DispatchError(f"active template {template_id} no longer loads: {exc}") from None


def _active_verifiers(active: Mapping[str, Any]) -> Mapping[str, Any]:
    item = active.get("verifier")
    return {} if item is None else {e["verifier_id"]: e for e in item.body["verifiers"]}


def active_verifier_entries(active: Mapping[str, Any]) -> tuple[int, tuple[Mapping[str, Any], ...]]:
    """``(generation, entries)`` of the active ``verifier`` item in ``active`` (kind -> its item, as
    ``ControlStore.active`` returns it); ``(0, ())`` when none is accepted. A stored 0.11 ``verifiers`` item is not
    among them."""
    item = active.get("verifier")
    if item is None:
        return 0, ()
    return item.generation, tuple(item.body["verifiers"])


def verifier_ticket(items: Mapping[str, Any], *, verifier_id: str) -> dict[str, Any]:
    """A verifier ticket's fields for ``verifier_id``, DERIVED from the active ``verifier`` item in ``items`` (kind ->
    its item): ``{verifier_generation, verifier_id, verifier_steps, driver_input_digest, entry}``, the generation
    the item's, the steps and the driver input digest copied from its entry, ``entry`` the whole activated entry (a
    copy). Nothing a planner or a job supplies reaches them. Raises :class:`DispatchError` when no verifier item is
    active, it has no such id, or the installed driver template is not the one the entry pins (or cannot be read)."""
    item = items.get("verifier")
    if item is None:
        legacy = legacy_verifiers_problem(items)
        raise DispatchError("no verifier item is active" + (f"; {legacy}" if legacy else ""))
    entry = next((e for e in item.body["verifiers"] if e["verifier_id"] == verifier_id), None)
    if entry is None:
        raise DispatchError(f"verifier {str(verifier_id)[:64]!r} is not in the active verifier item (generation "
                            f"{item.generation})")
    # the installed template must still be the one the entry pins: an upgrade that changed it mints no ticket
    try:
        shipped = driver_template_digest(entry["driver_template"])
    except OSError as exc:
        raise DispatchError(f"verifier {entry['verifier_id']}'s driver template {entry['driver_template']} cannot "
                            f"be read ({type(exc).__name__})") from None
    if shipped != entry["driver_template_digest"]:
        raise DispatchError(f"verifier {entry['verifier_id']}'s driver template {entry['driver_template']} "
                            + ("is not shipped by this levain" if shipped is None else
                               "installed is not the one the active verifier item pins")
                            + f"; {RESIGN_VERIFIERS}")
    entry = parse(canonical_bytes(entry))         # a copy: the stored item's body is never handed out
    return {"verifier_generation": item.generation, "verifier_id": entry["verifier_id"],
            "verifier_steps": entry["steps"], "driver_input_digest": entry["driver_input_digest"], "entry": entry}


def verifier_item_refusal(item: bytes, signature: Any) -> str | None:
    """Why a verifier item and its signature cannot reach CONTROL in one ``CONFIG-SUBMIT`` frame, else ``None``: S1's
    own bounds (:data:`levain.slot.routemaps.VERIFIER_ITEM_MAX_BYTES`, ``VERIFIER_SIG_MAX``), so dispatch never accepts
    a verifier configuration CONTROL cannot be handed."""
    if len(item) > VERIFIER_ITEM_MAX_BYTES:
        return (f"the verifier item is {len(item)} bytes, over the {VERIFIER_ITEM_MAX_BYTES} one CONFIG-SUBMIT frame "
                "to CONTROL carries")
    if not isinstance(signature, str) or not signature.isascii() \
            or len(canonical_bytes(signature)) > VERIFIER_SIG_MAX:
        return f"a verifier item's signature is ASCII and at most {VERIFIER_SIG_MAX} bytes as JSON"
    return None


def body_refusal(kind: str, body: Any, active: Mapping[str, Any]) -> str | None:
    """Why ``body`` cannot be the next ``kind`` item given the ``active`` configuration (kind -> its accepted item,
    each with a ``body``); ``None`` when it can."""
    if kind == "templates":
        return _templates(body, _active_verifiers(active))
    if kind == "verifier":
        return _verifier(body)
    if kind in LEGACY_KINDS:
        return f"kind {kind!r} is Levain 0.11's verifier shape and is no longer accepted; {RESIGN_VERIFIERS}"
    if kind == "provider_policy":
        return _provider_policy(body)
    if kind == "routemap":
        return _routemap(body)
    if kind == "adoption_rules":
        return _adoption_rules(body, active)
    return f"kind {kind!r} is not one of {KINDS}"


# --- readers -------------------------------------------------------------------------------------------------------

def resolver(control_store: _ConfigStore) -> Callable[[str], ResolvedTemplate | None]:
    """The active templates as ``JobStore.file`` takes them (``resolve_template``): an id resolves to its template,
    or ``None`` when no active template has it. Each call reads the store afresh; a store that cannot be read or
    verified, a template the current rules refuse, or one whose verifier no longer agrees with the active
    verifiers, RAISES (resolution then refuses the contract), never resolves."""
    def resolve(template_id: str) -> ResolvedTemplate | None:
        active = control_store.active()
        template = _active_template(active, template_id)
        if template is None:
            return None
        problem = _verifier_mismatch(template, _active_verifiers(active))
        if problem is not None:
            raise DispatchError(problem)
        return template
    return resolve


@dataclass(frozen=True)
class AdoptionRule:
    """An adoption rule as :func:`active_adoption_rules` reads it: judged against the active configuration."""
    repo: str
    template_id: str
    template_hash: str
    verifier_kind: str
    # the typed criteria (F11 §5) as (key, value) pairs, in the order written
    criteria: tuple[tuple[str, Any], ...]


def active_adoption_rules(control_store: _ConfigStore) -> tuple[AdoptionRule, ...]:
    """The active adoption rules, each re-judged against the templates and verifiers active NOW (R0, black-box,
    bound to the active template's content hash, its verifier still the registered one). Raises when the store
    cannot be read or verified or any rule no longer holds: a rule is never read out against content it was not
    signed for. No rules accepted reads as none, which adopts nothing."""
    return adoption_rules_at(control_store)[1]


def adoption_rules_at(control_store: _ConfigStore) -> tuple[int, tuple[AdoptionRule, ...]]:
    """:func:`active_adoption_rules` with the generation of the item they come from (0 when none is accepted), both
    from ONE verified read, so a land records the generation of the rules it was judged by."""
    active = control_store._verified_items()
    item = active.get("adoption_rules")
    if item is None:
        return 0, ()
    problem = _adoption_rules(item.body, active)
    if problem is not None:
        raise DispatchError(f"the active adoption rules no longer hold: {problem}")
    out = []
    for r in item.body:
        template = _active_template(active, r["template_id"])
        assert template is not None                  # _adoption_rules just judged it active
        out.append(AdoptionRule(repo=r["repo"], template_id=r["template_id"], template_hash=r["template_hash"],
                                verifier_kind=r["verifier_kind"],
                                criteria=parse_criteria(r["criteria"], template.write_paths)))
    return item.generation, tuple(out)


def active_policy(control_store: _ConfigStore) -> ProviderPolicy:
    """The active provider policy; raises when the store cannot be read or verified, or no policy is accepted yet
    (no policy is never an empty one)."""
    item = control_store.active().get("provider_policy")
    if item is None:
        raise DispatchError("no provider policy has been accepted; nothing may be dispatched")
    return ProviderPolicy.from_mapping(item.body)
