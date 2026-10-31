"""levain.dream.executor — the store-path consolidate core (D1_EXECUTOR §2, BUILD_PLAN D1-1a).

``consolidate_store`` is ``levain.wrap._consolidate`` lifted off the entity: it takes a STORE path (the ``memory.db``
of a store this caller owns), a compose ``mind`` and the effect's hooks, and runs one journaled effect:

    lock → (orphan self-heal) → snapshot hook → prepare → compose loop (check on a scratch copy, re-prompt with every
    refusal, §2.4's start rule) → progress.json ``saving`` → save → on_saved hook → usage.json

``levain wrap`` calls it with one compose and no check (``max_composes=1``), and keeps the entity guard and the
entity constitution; its messages, exit codes and compose route are unchanged.

``consolidate_store`` never raises for an outcome it can name (the journal poisons an effect that raises): it returns a
:class:`ConsolidateResult` whose ``outcome`` is one of :data:`OUTCOMES`. Out-of-band exits (``TurnTimeout`` from a
caller's deadline, ``KeyboardInterrupt``) cancel our wrap by token and propagate, exactly as ``wrap.py`` does.

NOT here (D1-1s's, after P5c-3): the ``Executor`` protocol, ``max_exposure_usd_micros`` and ``stop()``. This module
imports nothing from ``levain/agents/accounting.py`` and defines no ``StopResult``. It keeps what ``stop()`` will read:
the ``progress.json`` keys, and the ``claude`` child started as an unreaped ``Popen`` leading its own process group,
held by the fire, which signals it only through that handle.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from levain import wrap as _w
from levain.firing.crystallization import refuse_crystallization
from levain.firing.deadline import TurnDeadline, TurnTimeout
from levain.firing.isolation import IsolationError  # noqa: F401  (re-exported for callers building a guard)

_log = logging.getLogger("levain.dream.executor")

ComposeUnavailable = _w._ComposeUnavailable

# ---------------------------------------------------------------------------------------------- numbers (§2.2, §2.4)

#: ``R``, the per-run limit at list price (policy, §2.4), in integer micro-dollars.
PER_RUN_LIMIT_USD_MICROS = 20_000_000
#: The cheapest compose measured ($1.62): a compose starts only if its cap is at least this (§2.4).
MIN_COMPOSE_USD_MICROS = 1_620_000
#: Flow's dream's pinned model (``DEFAULT_MODEL``).
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
MAX_COMPOSES = 3
#: Flow's ``SAVE_RESERVE_S``: a compose starts only if ``max_seconds - elapsed`` leaves this for the save.
SAVE_RESERVE_S = 960.0
#: Flow's ``DREAM_BUDGET_S``.
DREAM_BUDGET_S = 3000.0
CHILD_TERM_WAIT_S = 5.0
CHILD_GROUP_WAIT_S = 5.0


@dataclass(frozen=True)
class PriceRow:
    """One model's row of the price table: the limits the API enforces on ONE request and the rates that bound one
    call's charge. ``input_micros_per_mtok`` is the model's HIGHEST per-token input rate (cache writes are charged above
    plain input, so the cache-write rate), ``output_micros_per_mtok`` its output rate; both in micro-dollars per
    million tokens. ``source`` is Anthropic's published price page and ``read_on`` the date a person read it there."""
    context_window: int
    max_output: int
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    source: str
    read_on: str


#: Keyed by the EXACT pinned model id. **Deliberately empty in D1-1a:** every row must cite the published price page
#: and the date it was read, and this lane has no network and may not invent a price. The person who runs D1-1a's first
#: Claude-route run adds the row for :data:`DEFAULT_CLAUDE_MODEL` (``PriceRow(..., source=<url>, read_on=<date>)``).
#: A model id with no row has no ``M``: the Claude mind refuses to compose and the effect returns ``budget``.
PRICE_TABLE: dict[str, PriceRow] = {}


def one_call_ceiling(model: str, table: Mapping[str, PriceRow] | None = None) -> int | None:
    """``M = W × p_in_max + O × p_out`` in micro-dollars (rounded UP), or ``None`` for a model with no price row."""
    row = (PRICE_TABLE if table is None else table).get(model)
    if row is None:
        return None
    total = row.context_window * row.input_micros_per_mtok + row.max_output * row.output_micros_per_mtok
    return -(-total // 1_000_000)


@dataclass(frozen=True)
class Budget:
    """The dollar bound of a Claude-route run (§2.4): ``limit`` is ``R``, ``m`` the one-call ceiling (``None``: no price
    row, nothing may start). Pure arithmetic, no I/O."""
    limit_usd_micros: int = PER_RUN_LIMIT_USD_MICROS
    m_usd_micros: int | None = None
    min_compose_usd_micros: int = MIN_COMPOSE_USD_MICROS

    @classmethod
    def for_model(cls, model: str, table: Mapping[str, PriceRow] | None = None,
                  limit_usd_micros: int = PER_RUN_LIMIT_USD_MICROS) -> "Budget":
        return cls(limit_usd_micros=limit_usd_micros, m_usd_micros=one_call_ceiling(model, table))

    def next_cap(self, spent_usd_micros: int) -> int | None:
        """``R − M − spent`` when a compose may start on it (at least the cheapest compose measured), else ``None``."""
        if self.m_usd_micros is None:
            return None
        cap = self.limit_usd_micros - self.m_usd_micros - spent_usd_micros
        return cap if cap >= self.min_compose_usd_micros else None


def charged_after(cap_usd_micros: int, m_usd_micros: int | None, usd_micros: int | None) -> int:
    """What a finished compose adds to ``spent so far``: its ``total_cost_usd``; a compose whose cost cannot be read
    counts as its cap plus ``M``, so no compose starts after it (§2.4)."""
    if usd_micros is not None:
        return usd_micros
    return cap_usd_micros + (m_usd_micros or 0)


# ------------------------------------------------------------------------------------- the progress.json schema (§2.2)

PROGRESS_KEYS = ("wrap_token", "phase", "last_wrap_before", "composed_sha256", "wrap_id",
                 "child_pid", "child_started_at", "child_argv")
PHASES = ("composing", "saving", "saved")


class Progress:
    """``<occurrence dir>/progress.json``, always carrying exactly :data:`PROGRESS_KEYS` (``null`` until set), written
    atomically (temp name, then rename). With no occurrence dir it keeps the values and writes nothing."""

    def __init__(self, occ_dir: Path | None) -> None:
        self.path = None if occ_dir is None else Path(occ_dir) / "progress.json"
        self.data: dict[str, Any] = dict.fromkeys(PROGRESS_KEYS)
        self.write_error: str | None = None

    def update(self, **values: Any) -> bool:
        """Set keys and write the file; False when it could not be written (never raises)."""
        unknown = set(values) - set(PROGRESS_KEYS)
        if unknown:
            raise KeyError(f"not a progress.json key: {sorted(unknown)}")
        if values.get("phase") is not None and values["phase"] not in PHASES:
            raise ValueError(f"not a progress phase: {values['phase']!r}")
        self.data.update(values)
        if self.path is None:
            return True
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            return False
        self.write_error = None
        return True


# ------------------------------------------------------------------------------------------------ hooks (§2.2)

#: ``on_snapshot(store, occ_dir) -> manifest``: D1-1b's snapshot. Raising refuses the effect before ``prepare``.
SnapshotHook = Callable[[Any, "Path | None"], Any]
#: ``on_saved(store, occ_dir, wrap_id) -> post_files``: D1-1b's record-post. Raising is recorded, never fatal.
SavedHook = Callable[[Any, "Path | None", Any], Any]


def _no_snapshot(store: Any, occ_dir: Path | None) -> Any:
    return None


def _no_saved(store: Any, occ_dir: Path | None, wrap_id: Any) -> Any:
    return None


# ------------------------------------------------------------------------------------------------ the typed result

OUTCOMES = ("saved", "refused", "budget", "empty", "lock-held", "snapshot-refused", "deadline", "dry-run",
            "precondition", "compose-unavailable", "compose-failed", "committed-unwritten", "unknown", "failed")

_ZERO_OUTCOMES = ("saved", "empty", "dry-run")


@dataclass
class ConsolidateResult:
    """``fn``'s typed, JSON-encodable result for every classified outcome. ``exit_code`` is ``wrap``'s (0 clean, 2 a
    precondition with nothing started, 1 a wrap this call started that did not complete cleanly)."""
    outcome: str
    exit_code: int
    detail: str = ""
    wrap_token: str | None = None
    wrap_id: Any = None
    chars: int | None = None
    refusals: list[str] = field(default_factory=list)
    composes: list[dict[str, Any]] = field(default_factory=list)
    spent_usd_micros: int = 0
    manifest: Any = None
    post_files: Any = None
    hook_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ------------------------------------------------------------------------------------------------ the compose minds

@dataclass
class ComposeRequest:
    package_text: str
    instructions: str
    constitution: str | None = None
    occ_dir: Path | None = None
    attempt: int = 1
    refusals: list[str] = field(default_factory=list)   # every refusal so far, verbatim
    prior_draft_path: Path | None = None
    cap_usd_micros: int | None = None                    # --max-budget-usd for this compose (Claude route)
    timeout_s: float | None = None
    progress: Progress | None = None


@dataclass
class ComposeResult:
    text: str = ""
    usd_micros: int | None = None        # list-price dollars; None = unreadable (Claude) or unmapped (Ollama)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    models: list[str] = field(default_factory=list)   # the models that CHARGED, read back from the result json
    model: str | None = None
    seconds: float = 0.0
    unit: str = "none"                   # "usd_list" | "tokens"
    error: str | None = None             # set when no text came back (budget, timeout, child failure)
    warnings: list[str] = field(default_factory=list)


def _int_or_none(v: Any) -> int | None:
    return v if type(v) is int and v >= 0 else None


def usd_to_micros(v: Any) -> int | None:
    """A ``total_cost_usd`` as integer micro-dollars; None for anything that is not a finite non-negative number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return round(v * 1_000_000)


def _group_signal(leader: Any, sig: int, *, group: bool) -> None:
    """One signal to the child (or its group) THROUGH the held handle: while the leader is an unreaped zombie its pid
    cannot be reused, so the signal can reach only this child's processes."""
    if not leader.hold_for_signal():
        return
    try:
        (os.killpg if group else os.kill)(leader.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    finally:
        leader.signal_sent()


def end_child(leader: Any, *, term_wait: float = CHILD_TERM_WAIT_S, group_wait: float = CHILD_GROUP_WAIT_S) -> bool:
    """End the compose child through its handle: SIGTERM the leader, SIGKILL it past ``term_wait``, SIGKILL the group
    while the zombie leader still reserves its number, poll the group gone, reap LAST and only then. True when the group
    was seen gone and the leader reaped; False (leader left unreaped, its number still its own) otherwise."""
    from levain.firing.confinement import group_gone_proc

    if leader.reaped:
        return True
    if not leader.exited:
        _group_signal(leader, signal.SIGTERM, group=False)
        if not leader.wait(term_wait):
            _group_signal(leader, signal.SIGKILL, group=False)
            leader.wait(term_wait)
    _group_signal(leader, signal.SIGKILL, group=True)
    if not group_gone_proc(leader.pid, timeout=group_wait):
        return False
    return bool(leader.reap(timeout=group_wait))


class ClaudeMind:
    """The ``claude -p`` mind (§2.3): flow's argv, started as an unreaped ``Popen`` that leads its own process group.

    The fire holds the handle (a ``confinement._Leader``, which reads the exit WITHOUT reaping) until it has seen the
    group gone, and signals the child only through it. ``progress.json`` gets ``child_pid``, ``child_started_at`` (the
    ``ps`` start time, as ``levain.consent.delivery.process_identity`` reads it) and ``child_argv`` as soon as the child
    is started, and the child keys are cleared once the child is reaped. A model id with no price row has no cap, and
    this mind REFUSES to compose without one."""

    name = "claude"
    source = "dream:claude-p"
    unit = "usd_list"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, *, claude_bin: str = "claude",
                 env: Mapping[str, str] | None = None) -> None:
        self.model = model
        self.claude_bin = claude_bin
        self.env = None if env is None else dict(env)

    def argv(self, occ_dir: Path, cap_usd_micros: int) -> list[str]:
        return [self.claude_bin, "-p", "--restricted", "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                "--strict-mcp-config", "--permission-mode", "dontAsk", "--output-format", "json",
                "--add-dir", str(occ_dir), "--model", self.model,
                "--max-budget-usd", f"{cap_usd_micros / 1_000_000:.6f}", "--no-session-persistence"]

    @staticmethod
    def prompt(req: ComposeRequest, package_path: Path) -> str:
        parts = []
        if req.constitution:
            parts.append(req.constitution + "\n\n---\n")
        parts.append(req.instructions)
        parts.append(f"\nThe consolidation package is the file {package_path}. Read it with the Read tool, then "
                     "write the memory document. Output ONLY the Markdown document, no preamble and no code fences.")
        if req.refusals:
            listing = "\n\n".join(f"[refusal {i}]\n{r}" for i, r in enumerate(req.refusals, 1))
            where = f" (your last draft is the file {req.prior_draft_path})" if req.prior_draft_path else ""
            parts.append(f"\nYour earlier draft(s) were REFUSED by the save{where}. Every refusal, verbatim; fix ALL "
                         f"of them in this one:\n\n{listing}")
        return "\n".join(parts)

    def compose(self, req: ComposeRequest) -> ComposeResult:
        from levain.consent.delivery import process_identity
        from levain.firing.confinement import _Leader

        if req.cap_usd_micros is None:
            raise ComposeUnavailable(f"no price row for {self.model!r}: the Claude mind does not compose without a "
                                     "budget cap")
        if req.occ_dir is None:
            raise ComposeUnavailable("the Claude mind needs an occurrence dir (--add-dir)")
        occ = Path(req.occ_dir)
        package_path = occ / "package.md"
        package_path.write_text(req.package_text, encoding="utf-8")
        argv = self.argv(occ, req.cap_usd_micros)
        stderr_path = occ / f"claude-{req.attempt}.stderr"
        started = time.monotonic()
        with open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                        start_new_session=True, env=self.env)
            except OSError as exc:
                raise ComposeUnavailable(f"cannot start {self.claude_bin!r}: {type(exc).__name__}: {exc}") from exc
        leader = _Leader(proc)
        chunks: list[bytes] = []
        threads: list[threading.Thread] = []
        try:
            ident = process_identity(proc.pid)
            if req.progress is not None:
                req.progress.update(child_pid=proc.pid, child_started_at=ident[0] if ident else None,
                                    child_argv=argv)
            data = self.prompt(req, package_path).encode("utf-8")

            def _feed() -> None:
                try:
                    proc.stdin.write(data)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        proc.stdin.close()

            def _drain() -> None:
                with contextlib.suppress(OSError, ValueError):
                    chunks.append(proc.stdout.read())

            for fn in (_feed, _drain):
                t = threading.Thread(target=fn, daemon=True)
                t.start()
                threads.append(t)
            exited = leader.wait(req.timeout_s if req.timeout_s is not None else 10 ** 9)
            if exited:
                threads[1].join(timeout=CHILD_GROUP_WAIT_S)
            seconds = time.monotonic() - started
        finally:
            ended = end_child(leader)
            if ended and req.progress is not None:
                req.progress.update(child_pid=None, child_started_at=None, child_argv=None)
            for t in threads:
                t.join(timeout=1.0)
            for stream in (proc.stdin, proc.stdout):
                with contextlib.suppress(Exception):
                    stream.close()
        if not exited:
            return ComposeResult(model=self.model, seconds=seconds, unit=self.unit, error="timeout")
        return self.parse(b"".join(chunks), leader.status, seconds)

    def parse(self, raw: bytes, status: int | None, seconds: float) -> ComposeResult:
        out = ComposeResult(model=self.model, seconds=seconds, unit=self.unit)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            doc = None
        if not isinstance(doc, dict):
            out.error = f"child-exit-{status}: no result json"
            return out
        out.usd_micros = usd_to_micros(doc.get("total_cost_usd"))
        usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
        out.input_tokens = _int_or_none(usage.get("input_tokens"))
        out.output_tokens = _int_or_none(usage.get("output_tokens"))
        out.cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
        out.cache_creation = _int_or_none(usage.get("cache_creation_input_tokens"))
        mu = doc.get("modelUsage")
        out.models = sorted(mu) if isinstance(mu, dict) else []
        stray = [m for m in out.models if m != self.model]
        if stray:
            out.warnings.append(f"a model other than the pinned {self.model!r} charged: {stray} (D1_EXECUTOR §2.4 "
                                "first-run fact 1: M gains that model's ceiling)")
        if doc.get("is_error") or (doc.get("subtype") not in (None, "success")):
            out.error = str(doc.get("subtype") or "is_error")
            return out
        text = doc.get("result")
        if not isinstance(text, str) or not text.strip():
            out.error = "no result text"
            return out
        out.text = _w._extract_neocortex(text)
        return out


def _response_usage(response: Any) -> tuple[int | None, int | None]:
    """``(prompt_tokens, completion_tokens)`` from a completion response, where the response carries them (``usage``
    first, then its raw response's, then the SDK's metrics); ``None`` for a count that is not there, never 0."""
    def pick(obj: Any, name: str) -> int | None:
        v = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        return _int_or_none(v)

    for holder in (response, getattr(response, "raw_response", None)):
        usage = holder.get("usage") if isinstance(holder, Mapping) else getattr(holder, "usage", None)
        if usage is not None:
            return pick(usage, "prompt_tokens"), pick(usage, "completion_tokens")
    metrics = getattr(response, "metrics", None)
    acc = getattr(metrics, "accumulated_token_usage", None) if metrics is not None else None
    if acc is not None:
        return pick(acc, "prompt_tokens"), pick(acc, "completion_tokens")
    return None, None


class OllamaMind:
    """The OpenHands/Ollama mind (§2.3, the entity's own model): wrap.py's compose route, returning ``response.usage``.

    Cost unit: tokens. Its dollars are UNMAPPED (litellm: *"This model isn't mapped yet"*), so ``usd_micros`` is
    ``None``, never 0 (R1's warning), and the result says so. ``client(system, user) -> response`` is injectable (the
    tests' fake); the default builds the OpenHands ``LLM`` the way ``wrap._compose`` does."""

    name = "ollama"
    source = "dream:ollama"
    unit = "tokens"

    def __init__(self, composer: str = _w.DEFAULT_COMPOSE_MODEL, *, base_url: str = "http://localhost:11434",
                 api_key: str | None = None, client: Callable[[str, str], Any] | None = None) -> None:
        self.model = composer
        self.base_url = base_url
        self.api_key = api_key
        self._client = client

    def _default_client(self, system: str, user: str) -> Any:
        try:
            from openhands.sdk import LLM, Message, TextContent
        except ImportError as exc:
            raise ComposeUnavailable("the OpenHands runtime is not installed — it is needed to run the compose model."
                                     f"\n  Install the extra:  pip install 'levain[openhands]'\n  ({exc})") from exc
        llm = LLM(usage_id="levain-dream", max_output_tokens=_w._COMPOSE_MAX_OUTPUT_TOKENS,
                  **_w._resolve_llm_kwargs(self.model, self.base_url, self.api_key))
        return llm.completion(messages=[Message(role="system", content=[TextContent(text=system)]),
                                        Message(role="user", content=[TextContent(text=user)])])

    def compose(self, req: ComposeRequest) -> ComposeResult:
        system = (req.constitution + "\n\n---\n\n" if req.constitution else "") + req.instructions
        user = req.package_text
        if req.refusals:
            user += ("\n\n---\nYour earlier draft(s) were REFUSED by the save. Every refusal, verbatim; fix ALL of them:\n\n"
                     + "\n\n".join(f"[refusal {i}]\n{r}" for i, r in enumerate(req.refusals, 1)))
        started = time.monotonic()
        response = (self._client or self._default_client)(system, user)
        inp, outp = _response_usage(response)
        out = ComposeResult(model=self.model, seconds=time.monotonic() - started, unit=self.unit,
                            input_tokens=inp, output_tokens=outp)
        out.warnings.append(f"dollars unmapped for {self.model!r}: tokens are reported, never $0")
        out.text = _w._extract_neocortex(_w._completion_text(response))
        if not out.text.strip():
            out.error = "no result text"
        return out


# ---------------------------------------------------------------------------------------- the check on a scratch copy

def _copy_store(store_path: Path, dest_dir: Path) -> Path:
    """A scratch copy of the store: the db by sqlite online backup (WAL-consistent) and every ``<stem>.*`` sibling
    (crystal, continuity text, baton, audit), never the lock files. Returns the copy's db path."""
    stem = store_path.stem
    dest_db = dest_dir / store_path.name
    src = sqlite3.connect(str(store_path))
    try:
        dst = sqlite3.connect(str(dest_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for p in store_path.parent.iterdir():
        if p.name != store_path.name and p.name.startswith(stem + ".") \
                and not p.name.endswith((".lock", "-wal", "-shm", ".tmp")):
            if p.is_file():
                shutil.copy2(p, dest_dir / p.name)
    return dest_db


def check_on_copy(store_path: Path, text: str, *, wrap_token: str, session_id: str | None, project_name: str,
                  unattended: bool = True) -> str | None:
    """Flow's ``save --check``, ported: run the WHOLE save (``validated_save_continuity``) on a scratch copy of the
    store, writing nothing to the real one. Returns None when the copy accepted ``text``, else the refusal text,
    verbatim. A copy that cannot be made returns None (the real save fails closed on its own)."""
    from anneal_memory import CrystalStore, Store
    from anneal_memory.continuity import validated_save_continuity

    tmp = Path(tempfile.mkdtemp(prefix="levain-dream-check-"))
    try:
        try:
            copy_db = _copy_store(store_path, tmp)
        except (OSError, sqlite3.Error) as exc:
            _log.warning("check on a copy skipped, the copy could not be made (%s: %s)", type(exc).__name__, exc)
            return None
        crystal = CrystalStore(copy_db.with_name(copy_db.stem + ".crystal.json"))
        if unattended:
            crystal = refuse_crystallization(crystal)  # type: ignore[assignment]
        cstore = Store(str(copy_db), project_name=project_name, section_schema=None)
        try:
            validated_save_continuity(cstore, text, wrap_token=wrap_token, crystal_store=crystal,
                                      allow_shrink=False, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 — every refusal is the check's answer
            return f"{type(exc).__name__}: {exc}"
        finally:
            cstore.close()
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------ usage.json

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Ctx:
    """What one effect has spent and composed so far, read by ``usage.json`` on every terminal path."""

    def __init__(self, occ_dir: Path | None, mind: Any, budget: Budget | None) -> None:
        self.occ_dir = occ_dir
        self.mind = mind
        self.budget = budget
        self.composes: list[dict[str, Any]] = []
        self.spent = 0
        self.outcome: str | None = None
        self.instructions_sha256: str | None = None


def _write_usage(ctx: _Ctx) -> None:
    if ctx.occ_dir is None:
        return
    unit = getattr(ctx.mind, "unit", "tokens")
    readable = all(c["usd_micros"] is not None for c in ctx.composes) if unit == "usd_list" else \
        all(c["input_tokens"] is not None and c["output_tokens"] is not None for c in ctx.composes)
    doc: dict[str, Any] = {
        "occurrence": ctx.occ_dir.name, "unit": unit,
        "reserved_usd_micros": ctx.budget.limit_usd_micros if ctx.budget is not None else None,
        "actual_usd_micros": (sum(c["usd_micros"] for c in ctx.composes) if unit == "usd_list" and readable
                              else None),
        "usd_mapped": unit == "usd_list",
        "input_tokens": sum(c["input_tokens"] or 0 for c in ctx.composes) if readable else None,
        "output_tokens": sum(c["output_tokens"] or 0 for c in ctx.composes) if readable else None,
        "readable": readable, "composes": ctx.composes, "outcome": ctx.outcome,
        "instructions_sha256": ctx.instructions_sha256,
        "source": getattr(ctx.mind, "source", "dream:unknown"),
    }
    try:
        tmp = ctx.occ_dir / "usage.json.tmp"
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        os.replace(tmp, ctx.occ_dir / "usage.json")
    except OSError as exc:
        _log.warning("usage.json could not be written (%s: %s)", type(exc).__name__, exc)


def _compose_row(n: int, res: ComposeResult) -> dict[str, Any]:
    return {"n": n, "usd_micros": res.usd_micros, "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
            "cache_read": res.cache_read, "cache_creation": res.cache_creation, "seconds": round(res.seconds, 3),
            "model": res.model, "models_charged": res.models, "error": res.error, "warnings": res.warnings}


def _crystal_path(store_path: Path) -> Path:
    return store_path.parent / (store_path.stem + ".crystal.json")


def _lock_path(store_path: Path) -> Path:
    return store_path.parent / "wrap.lock"


def _default_report(saved: Mapping[str, object]) -> None:
    print("\nlevain dream: consolidated.")
    print(f"  memory:       {saved.get('path')}")
    print(f"  chars:        {saved.get('chars')}")
    print(f"  episodes:     {saved.get('episodes_compressed')} compressed")
    if saved.get("wrap_id") is not None:
        print(f"  wrap:         #{saved.get('wrap_id')}")


# ------------------------------------------------------------------------------------------- consolidate_store (§2.2)

def consolidate_store(
    store_path: Path | str,
    *,
    mind: Any,
    composer: str | None = None,
    max_seconds: float | None = None,
    save_reserve: float = SAVE_RESERVE_S,
    budget: Budget | None = None,
    instructions_path: Path | str | None = None,
    occurrence_dir: Path | str | None = None,
    on_snapshot: SnapshotHook | None = None,
    on_saved: SavedHook | None = None,
    max_composes: int = MAX_COMPOSES,
    dry_run: bool = False,
    reset: bool = False,
    unattended: bool = True,
    own_deadline: bool = True,
    hard_exit_code: int = 5,
    label: str = "levain dream",
    project_name: str | None = None,
    constitution: str | None = None,
    report: Callable[[Mapping[str, object]], None] | None = None,
    acquire_lock: Callable[[], Any] | None = None,
    read_wrap_schema: Callable[[Any], Any] | None = None,
) -> ConsolidateResult:
    """One consolidate on the store at ``store_path`` (a ``memory.db``). See the module docstring.

    ``mind`` is a compose mind (``.compose(ComposeRequest) -> ComposeResult``); ``composer`` only names the model in
    the "consolidating …" line. ``budget`` is the Claude route's dollar bound (None: no dollar cap, e.g. Ollama).
    ``max_composes=1`` is ``levain wrap``'s single compose with no check. ``own_deadline`` arms a ``TurnDeadline`` over
    steps 1-4 only (the save runs under ``save_reserve``); ``levain wrap`` passes False because its caller bounds the
    whole consolidate. ``label`` prefixes every message.

    ``acquire_lock() -> handle`` and ``read_wrap_schema(store) -> schema`` are ``levain wrap``'s own seams: it passes
    functions defined in ``levain/wrap.py``, so the patch points its existing tests use (``wrap._lock_wrap``, a store
    read made from a ``levain.wrap`` frame) still bind. The defaults are the store-path core's own.
    """
    store_path = Path(store_path)
    occ_dir = None if occurrence_dir is None else Path(occurrence_dir)
    ctx = _Ctx(occ_dir, mind, budget)
    progress = Progress(occ_dir)
    try:
        if occ_dir is not None:
            occ_dir.mkdir(parents=True, exist_ok=True)
            progress.update()
        res = _consolidate_store(
            ctx, progress, store_path, mind=mind, composer=composer, max_seconds=max_seconds,
            save_reserve=save_reserve, budget=budget, instructions_path=instructions_path,
            on_snapshot=on_snapshot or _no_snapshot, on_saved=on_saved or _no_saved, max_composes=max_composes,
            dry_run=dry_run, reset=reset, unattended=unattended, own_deadline=own_deadline,
            hard_exit_code=hard_exit_code, label=label, project_name=project_name or store_path.parent.name,
            constitution=constitution, report=report or _default_report,
            acquire_lock=acquire_lock, read_wrap_schema=read_wrap_schema)
        ctx.outcome = res.outcome
        res.composes = list(ctx.composes)
        res.spent_usd_micros = ctx.spent
        return res
    finally:
        _write_usage(ctx)


def _consolidate_store(ctx: _Ctx, progress: Progress, store_path: Path, *, mind: Any, composer: str | None,
                       max_seconds: float | None, save_reserve: float, budget: Budget | None,
                       instructions_path: Path | str | None, on_snapshot: SnapshotHook, on_saved: SavedHook,
                       max_composes: int, dry_run: bool, reset: bool, unattended: bool, own_deadline: bool,
                       hard_exit_code: int, label: str, project_name: str, constitution: str | None,
                       report: Callable[[Mapping[str, object]], None],
                       acquire_lock: Callable[[], Any] | None, read_wrap_schema: Callable[[Any], Any] | None,
                       ) -> ConsolidateResult:
    started = time.monotonic()
    occ_dir = ctx.occ_dir
    crystal_path = _crystal_path(store_path)
    name = project_name
    L = label

    if not store_path.exists():
        print(f"{L}: {store_path.parent} has no memory store yet ({store_path} is missing) — nothing to consolidate.")
        return ConsolidateResult("precondition", 2, "no memory store")

    instructions_override: str | None = None
    if instructions_path is not None:
        try:
            instructions_override = Path(instructions_path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{L}: the instructions file cannot be read ({type(exc).__name__}: {exc}).")
            return ConsolidateResult("precondition", 2, "instructions unreadable")

    try:
        from anneal_memory import (
            FLOW_SCHEMA,
            AnnealMemoryError,
            name_for_schema,
            CrystalStore,
            Store,
            WrapInProgressError,
            WrapOwnershipError,
        )
        from anneal_memory.continuity import (
            format_wrap_package_text,
            prepare_wrap,
            validated_save_continuity,
        )

        from levain.manifest import ANNEAL_SESSION_ID
    except ImportError as exc:  # anneal-memory is a BASE dep, so this is a broken install
        print(f"{L}: anneal-memory is not importable ({exc}). Reinstall levain.")
        return ConsolidateResult("precondition", 2, "anneal not importable")

    try:
        store = Store(str(store_path), project_name=name, section_schema=None)
    except AnnealMemoryError as exc:
        print(f"{L}: could not open {name}'s memory store ({type(exc).__name__}: {exc}).")
        return ConsolidateResult("precondition", 2, "store unopenable")

    lock = acquire_lock() if acquire_lock is not None else _w._lock_wrap_file(_lock_path(store_path))
    if lock is _w._ANOTHER_WRAP_RUNNING:
        store.close()
        print(f"{L}: another consolidate is already running on this store ({_lock_path(store_path)} is held). "
              "Wait for it to finish, then re-run.")
        return ConsolidateResult("lock-held", 2, "another consolidate running")

    wrap_token = uuid.uuid4().hex
    prepare_entered = False
    stack = contextlib.ExitStack()
    deadline: TurnDeadline | None = None
    if own_deadline and max_seconds:
        deadline = TurnDeadline(max_seconds, hard_exit_code=hard_exit_code,
                                hard_report=lambda s: _w.format_wrap_timeout_report(s, hard=True))
        stack.enter_context(deadline)

    def _fail(outcome: str, code: int, detail: str, **kw: Any) -> ConsolidateResult:
        return ConsolidateResult(outcome, code, detail, wrap_token=wrap_token if prepare_entered else None, **kw)

    try:
        headings = [s["heading"] for s in store.section_schema]
        expected = [s["heading"] for s in FLOW_SCHEMA if not s.get("optional")]
        if name_for_schema(store.section_schema) != "partnership":
            from levain.manifest import anneal_invocation

            print(f"{L}: this store is not on the partnership schema, so it cannot consolidate.\n"
                  f"  got:      {headings}\n  required: {expected}\n"
                  f"  Fix:  {anneal_invocation('--db', str(store_path), 'set-schema', 'partnership')}")
            return _fail("precondition", 2, "not the partnership schema")

        holds_lock = isinstance(lock, int)
        prior_token, started_at = _w._observe_prior_wrap(store)
        may_self_heal = unattended and holds_lock and _w._orphan_is_stale(started_at, max_seconds)
        if started_at:
            if not reset and not may_self_heal:
                extra = ""
                if unattended and holds_lock and not _w._orphan_is_stale(started_at, max_seconds):
                    extra = ("\n  (This run would normally discard the orphan itself, but that wrap is too RECENT to "
                             "be provably dead — another writer may still be mid-consolidate on this store. It will be "
                             "discarded on a later run once it ages past the bound; refusing now is the safe side.)")
                elif unattended and not holds_lock:
                    extra = ("\n  (This run would normally discard the orphan itself, but no wrap lock could be taken "
                             "on this platform, so it cannot prove the wrap is not a live peer's. Refusing is the safe "
                             "side.)")
                print(f"{L}: a prior wrap is still in progress (started {started_at}).\n"
                      "  A previous consolidate did not finish. Re-run with --reset to discard it and start fresh — "
                      "your captured episodes are safe (they return to the next wrap)." + extra)
                return _fail("precondition", 2, "prior wrap in progress")
            try:
                discarded = _w._discard_prior_wrap(store, prior_token)
            except WrapOwnershipError:
                from levain.manifest import anneal_invocation

                print(f"{L}: the prior wrap was not cleared: another consolidate replaced it, or its metadata cannot "
                      "be read. Nothing was changed. Re-run in a moment; if it persists, look at it with\n"
                      f"  {anneal_invocation('--db', str(store_path), 'wrap-status')}\n"
                      "and clear it ONLY once you have confirmed it is dead: a wrap in partial state with\n"
                      f"  {anneal_invocation('--db', str(store_path), 'wrap-cancel', '--partial')}\n"
                      "or a readable one with that same command and `--wrap-token <its token>` in place of "
                      "`--partial`. If the status shows a token that is not 32 hex characters, anneal will not take it "
                      "as `--wrap-token`: the same command with no flag then clears whatever wrap is current, so run "
                      "it straight after wrap-status and only if that wrap is the dead one. Keep the `--db` shown "
                      "here: without it anneal reads ~/.anneal-memory.")
                return _fail("precondition", 2, "prior wrap not cleared")
            if not discarded:
                print(f"{L}: the prior wrap was already gone; continuing.")
            elif unattended and not reset:
                print(f"{L}: discarded an ORPHANED wrap from a prior run (started {started_at}) and continued — no "
                      "human is present to be asked.\n"
                      "  Episodes are safe (they return to this wrap). If this recurs every run, the consolidate is "
                      "dying repeatedly: check the log for a bound being hit.")
            else:
                print(f"{L}: discarded an unfinished prior wrap (started {started_at}).")

        crystal = CrystalStore(crystal_path)
        if unattended:
            crystal = refuse_crystallization(crystal)  # type: ignore[assignment]

        # Step 2: the snapshot hook, after any orphan is cleared and before anything is prepared.
        manifest = None
        if not dry_run:
            try:
                manifest = on_snapshot(store, occ_dir)
            except Exception as exc:  # noqa: BLE001 — a refused snapshot is a named outcome, nothing written yet
                print(f"{L}: the snapshot was refused ({type(exc).__name__}: {exc}); nothing was prepared or saved.")
                return _fail("snapshot-refused", 2, f"{type(exc).__name__}: {exc}")

        session_id = ANNEAL_SESSION_ID if store.consolidate_requires_baton() else None
        prepare_entered = True
        result = prepare_wrap(store, crystal_store=crystal, wrap_token=wrap_token, session_id=session_id)
        status = result.get("status")
        if status == "empty":
            print(f"{L}: nothing to consolidate — no new episodes since the last wrap.\n"
                  "  Talk to the entity with `levain run` to accumulate experience, then wrap.")
            return _fail("empty", 0, "no new episodes", manifest=manifest)
        if status != "ready":
            if status == "downgraded":
                print(f"{L}: anneal declined to start a wrap — {result.get('message') or status}")
                return ConsolidateResult("failed", 1, f"anneal declined: {result.get('message') or status}")
            print(f"{L}: prepare_wrap returned an unexpected status {status!r} — aborting.")
            if not _w._cancel_if_ours(store, wrap_token):
                print(_w._CANCEL_FAILED)
            return _fail("failed", 1, f"unexpected prepare status {status!r}", manifest=manifest)

        wrap_schema = read_wrap_schema(store) if read_wrap_schema is not None else store.section_schema_for_wrap()
        if name_for_schema(wrap_schema) != "partnership":
            cancelled = _w._cancel_if_ours(store, wrap_token)
            print(f"{L}: this store changed off the partnership schema while the consolidate was starting, so it was "
                  f"stopped; nothing was saved.\n  got:      {[s['heading'] for s in wrap_schema]}\n"
                  f"  required: {expected}")
            if not cancelled:
                print(_w._CANCEL_FAILED)
            return _fail("precondition", 2, "schema changed under the wrap", manifest=manifest)
        package_text = format_wrap_package_text(result)
        episode_count = result.get("episode_count")

        if dry_run:
            cancelled = _w._cancel_if_ours(store, wrap_token)
            print(f"{L}: DRY RUN — {episode_count} episode(s) are ready to consolidate (wrap_token {wrap_token}). "
                  "Nothing was composed or saved.\n")
            print(package_text)
            if not cancelled:
                print(_w._CANCEL_FAILED)
                return _fail("failed", 1, "dry-run wrap could not be cancelled")
            return _fail("dry-run", 0, f"{episode_count} episode(s) ready")

        progress.update(wrap_token=wrap_token, phase="composing")
        instructions = instructions_override if instructions_override is not None else \
            _w._compose_instructions(wrap_schema)
        ctx.instructions_sha256 = _sha256(instructions.encode("utf-8"))
        print(f"{L}: consolidating {episode_count} episode(s) into {name}'s memory with "
              f"{composer or getattr(mind, 'model', '?')} …")

        # Step 4: the compose loop.
        refusals: list[str] = []
        neocortex = ""
        prior_draft: Path | None = None
        stopped_by: str | None = None
        last_error: str | None = None
        last_was_check_refusal = False
        for attempt in range(1, max(1, max_composes) + 1):
            timeout_s = None
            if max_seconds:
                left = max_seconds - (time.monotonic() - started)
                if left < save_reserve:
                    stopped_by = "deadline"
                    break
                timeout_s = left - save_reserve
            cap = None
            if budget is not None:
                cap = budget.next_cap(ctx.spent)
                if cap is None:
                    stopped_by = "budget"
                    break
            req = ComposeRequest(package_text=package_text, instructions=instructions, constitution=constitution,
                                 occ_dir=occ_dir, attempt=attempt, refusals=list(refusals),
                                 prior_draft_path=prior_draft, cap_usd_micros=cap, timeout_s=timeout_s,
                                 progress=progress)
            try:
                res = mind.compose(req)
            except ComposeUnavailable as exc:
                cancelled = _w._cancel_if_ours(store, wrap_token)
                print(f"{L}: {exc}")
                if not cancelled:
                    print(_w._CANCEL_FAILED)
                return _fail("compose-unavailable", 2, str(exc))
            except Exception as exc:  # noqa: BLE001 — a model/endpoint failure is a run failure, not a crash
                cancelled = _w._cancel_if_ours(store, wrap_token)
                print(f"{L}: the compose model failed ({type(exc).__name__}: {exc}).\n"
                      "  Nothing was saved; the memory is unchanged and its episodes are safe. "
                      "Check --composer / --base-url and re-run.")
                if not cancelled:
                    print(_w._CANCEL_FAILED)
                return _fail("compose-failed", 1, f"{type(exc).__name__}: {exc}")
            ctx.composes.append(_compose_row(attempt, res))
            if cap is not None:
                ctx.spent += charged_after(cap, budget.m_usd_micros if budget else None, res.usd_micros)
            last_error = res.error or (None if res.text.strip() else "empty")
            last_was_check_refusal = False
            if last_error:
                refusals.append(f"the compose returned no usable memory text ({res.error or 'empty'})")
                continue
            neocortex = res.text
            if occ_dir is not None:
                prior_draft = occ_dir / f"draft-{attempt}.md"
                with contextlib.suppress(OSError):
                    prior_draft.write_text(neocortex, encoding="utf-8")
            if attempt < max_composes:
                refusal = check_on_copy(store_path, neocortex, wrap_token=wrap_token, session_id=session_id,
                                        project_name=name, unattended=unattended)
                if refusal is not None:
                    refusals.append(refusal)
                    neocortex = ""
                    last_was_check_refusal = True
                    continue
            break

        if not neocortex.strip():
            cancelled = _w._cancel_if_ours(store, wrap_token)
            if stopped_by is not None:
                why = {"budget": "the start rule left no compose a cap of at least the cheapest compose measured "
                                 "(or the model has no price row)",
                       "deadline": "the time left would not cover the save reserve"}[stopped_by]
                print(f"{L}: no compose was started or kept: {why}; nothing saved.")
                outcome = stopped_by
            elif last_was_check_refusal:
                print(f"{L}: every compose was refused by the check; nothing saved.\n  last refusal: {refusals[-1]}")
                outcome = "refused"
            elif last_error == "error_max_budget_usd":
                print(f"{L}: the compose reached its budget cap ({last_error}); nothing saved.")
                outcome = "budget"
            else:
                print(f"{L}: the compose model returned no usable memory text; nothing saved. "
                      "Re-run (a stronger --composer may help a weak model).")
                outcome = "compose-failed"
            if not cancelled:
                print(_w._CANCEL_FAILED)
            return _fail(outcome, 1, refusals[-1] if refusals else (stopped_by or "no text"),
                         refusals=refusals, manifest=manifest)

        # Steps 1-4 are over: the deadline's region ends here, the save runs under its reserve.
        stack.close()
        composed_sha = _sha256(neocortex.encode("utf-8"))
        if occ_dir is not None:
            with contextlib.suppress(OSError):
                (occ_dir / "composed.continuity.md").write_text(neocortex, encoding="utf-8")
                (occ_dir / "composed.continuity.md.sha256").write_text(composed_sha + "\n", encoding="utf-8")
        last_wrap_before = _w._last_wrap_id(store)
        if not progress.update(phase="saving", last_wrap_before=last_wrap_before, composed_sha256=composed_sha):
            cancelled = _w._cancel_if_ours(store, wrap_token)
            print(f"{L}: progress.json could not be written ({progress.write_error}); nothing saved.")
            if not cancelled:
                print(_w._CANCEL_FAILED)
            return _fail("failed", 1, f"progress.json unwritable: {progress.write_error}", manifest=manifest)

        try:
            saved = validated_save_continuity(store, neocortex, wrap_token=wrap_token, crystal_store=crystal,
                                              allow_shrink=False, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 — anneal raises ValueError (validation) / StoreError
            return _classify_failed_save(store, exc, wrap_token, last_wrap_before, neocortex, store_path, L,
                                         refusals, manifest)

        wrap_id = saved.get("wrap_id") if isinstance(saved, Mapping) else None
        progress.update(phase="saved", wrap_id=wrap_id)
        out = ConsolidateResult("saved", 0, "saved", wrap_token=wrap_token, wrap_id=wrap_id,
                                chars=saved.get("chars") if isinstance(saved, Mapping) else None,
                                refusals=refusals, manifest=manifest)
        try:
            out.post_files = on_saved(store, occ_dir, wrap_id)
        except Exception as exc:  # noqa: BLE001 — the memory is saved; recovery's record-post gap, not a failure
            out.hook_errors.append(f"on_saved: {type(exc).__name__}: {exc}")
        try:
            report(saved)
        except Exception as exc:  # noqa: BLE001
            _w._say(f"{L}: consolidated: the memory was SAVED, but showing the result failed "
                    f"({type(exc).__name__}: {exc}).\n  Nothing from this wrap is left to re-run.")
        return out
    except WrapInProgressError as exc:
        print(f"{L}: a wrap is already in progress ({exc}).\n"
              "  Another consolidate may be running, or a prior one crashed. If nothing else is running, re-run "
              "with --reset to discard the stale wrap (episodes are safe).")
        return _fail("precondition", 2, f"wrap in progress: {exc}")
    except AnnealMemoryError as exc:
        try:
            cancelled = _w._cancel_if_ours(store, wrap_token) if prepare_entered else True
        except BaseException as stop:
            _w._note_cancel_outcome(stop, _w._cancel_our_wrap(store, wrap_token))
            raise
        print(f"{L}: the consolidate could not read {name}'s store ({type(exc).__name__}: {exc}).")
        if not cancelled:
            print(_w._CANCEL_FAILED)
        return _fail("precondition", 2, f"{type(exc).__name__}: {exc}")
    except TurnTimeout as exc:
        if deadline is None:
            _w._note_cancel_outcome(exc, _w._cancel_our_wrap(store, wrap_token))
            raise
        outcome = _w._cancel_our_wrap(store, wrap_token)
        print(_w.format_wrap_timeout_report(deadline.seconds or 0.0, hard=False, outcome=outcome),
              file=sys.stderr, flush=True)
        return _fail("deadline", 5, f"the wall-clock bound fired (cancel: {outcome})")
    except BaseException as exc:
        _w._note_cancel_outcome(exc, _w._cancel_our_wrap(store, wrap_token))
        raise
    finally:
        stack.close()
        store.close()
        _w._unlock_wrap(lock)


def _classify_failed_save(store: Any, exc: Exception, wrap_token: str, last_wrap_before: int | None, neocortex: str,
                          store_path: Path, L: str, refusals: list[str], manifest: Any) -> ConsolidateResult:
    """``wrap.py``'s oracle, unchanged: what the STORE establishes after a save that raised, never the file's sha and
    never the absence of a flag. ``fn`` returns the classification; it never raises for a refused save."""
    last_wrap_after = _w._last_wrap_id(store)
    base = dict(wrap_token=wrap_token, manifest=manifest)
    if (last_wrap_before is not None and last_wrap_after is not None and last_wrap_after != last_wrap_before
            and "preserved at" in str(exc)):
        print(f"{L}: the memory COMMITTED but writing it to disk did not finish ({type(exc).__name__}: {exc}).\n"
              "  The consolidate IS recorded — follow the recovery hint in the message above to move the preserved "
              ".tmp into place. Do NOT re-run; these episodes are already consolidated.")
        return ConsolidateResult("committed-unwritten", 1, f"{type(exc).__name__}: {exc}", wrap_id=last_wrap_after,
                                 refusals=refusals, **base)
    if last_wrap_before is None or last_wrap_after is None:
        cancelled = _w._cancel_if_ours(store, wrap_token)
        print(_w._SAVE_UNREADABLE.format(kind=type(exc).__name__, exc=exc).replace("levain wrap", L))
        if not cancelled:
            print(_w._CANCEL_FAILED)
        return ConsolidateResult("unknown", 1, f"save unreadable: {type(exc).__name__}: {exc}", refusals=refusals,
                                 **base)
    if _w._wrap_in_progress(store):
        debug_path = store_path.parent / f"rejected-wrap.{wrap_token}.md"
        try:
            debug_path.write_text(neocortex, encoding="utf-8")
        except Exception as werr:  # noqa: BLE001 — a debug dump must not mask the real failure
            _log.debug("could not write rejected-wrap draft (%s): %s", type(werr).__name__, werr)
        cancelled = _w._cancel_if_ours(store, wrap_token)
        why = (("The composed memory is LONGER than the schema's hard maximum; the message above names what to cut."
                + (" Levain cancelled its wrap, so ignore the 'save again' line: re-run." if cancelled else ""))
               if type(getattr(exc, "bound", None)) is int else
               "The compose model most likely dropped a required section, mis-cited an episode, or (on a later wrap) "
               "produced a too-small memory that the catastrophic-shrink gate refused.")
        print(f"{L}: the composed memory was REFUSED — NOT saved ({type(exc).__name__}: {exc}).\n"
              f"  The identity is unchanged. {why}\n"
              f"  The rejected draft was saved for inspection: {debug_path}\n"
              "  Re-run to try again (a stronger --composer may help).")
        if not cancelled:
            print(_w._CANCEL_FAILED)
        return ConsolidateResult("refused", 1, f"{type(exc).__name__}: {exc}", refusals=refusals + [str(exc)], **base)
    if last_wrap_after == last_wrap_before and _w._unwrapped_count(store):
        print(f"{L}: the wrap was cleared by another program while the memory was being composed, so it was NOT saved "
              f"({type(exc).__name__}: {exc}).\n  The identity is unchanged and its episodes are still unconsolidated. "
              "Re-run.")
        return ConsolidateResult("failed", 1, "wrap cleared by another program", refusals=refusals, **base)
    print(_w._SAVE_UNKNOWN.format(kind=type(exc).__name__, exc=exc).replace("levain wrap", L))
    return ConsolidateResult("unknown", 1, f"save unknown: {type(exc).__name__}: {exc}", refusals=refusals, **base)
