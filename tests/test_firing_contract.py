"""Base-lane tests for ``levain.firing.contract`` — the drift-defense directive constant.

These are PURE-CONSTANT tests (no ``openhands`` extra), deliberately here and NOT in
``test_firing_condenser.py`` (which is ``pytest.importorskip("openhands.sdk")``-gated). The
spore-296 drift-lock is the guard that justifies the whole "package-locus directives" design —
it MUST run in the default/base test lane, so a base-only CI can't let the package directives
silently diverge from the template (apparatus L3: complement + anansi converged on this).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import levain
from levain.firing.contract import DIRECTIVES

_SHARED_TEMPLATE = (
    Path(levain.__file__).parent / "templates" / "activation" / "recency_directives.md"
)
_SHARED_HOOK = (
    Path(levain.__file__).parent / "templates" / "activation" / "hooks" / "_levain_hook.py"
)


def _real_read_blocks():
    """Load the ACTUAL ``_levain_hook.read_blocks`` the hooked adapters use, by file path (the
    ``templates/`` tree ships as package data, not an importable package). Locking against the real
    parser — not a hand-reimplementation — closes the exact silent-divergence the parity test exists
    to prevent (apparatus L3: complement + codex + kimi + anansi + L1 all converged; each verified
    the real function loads clean and returns the same blocks)."""
    spec = importlib.util.spec_from_file_location("_levain_hook_for_parity", _SHARED_HOOK)
    assert spec is not None and spec.loader is not None, _SHARED_HOOK
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_blocks


def test_directive_parity_with_shared_activation_template():
    """spore-296 drift-lock: the package ``DIRECTIVES`` (the HOOKLESS OpenHands entity's ONLY
    per-turn drift-defense surface) stay in sync with the SHARED activation template's block bodies
    (the HOOKED adapters' ``recency_directives.md``) — a package upgrade improves BOTH surfaces
    together (the package-locus benefit). Locks against the REAL ``read_blocks`` the hook uses, so a
    change to the parser's block-extraction can't leave the two surfaces silently divergent.

    Scope of the lock: the package DEFAULT directives vs the package DEFAULT template (both shipped
    in the wheel). A per-entity operator editing their OWN installed activation copy is expected and
    NOT governed here. Deliberately the SHARED (generic, broad-cross-model) template, NOT the
    Codex-lineage-tuned copy — the entity runs assorted open models; a substrate-tuned open-model set
    is a later sourdough-grow, at which point this lock re-points to that set."""
    assert _SHARED_TEMPLATE.exists(), _SHARED_TEMPLATE
    read_blocks = _real_read_blocks()
    assert list(DIRECTIVES) == read_blocks(_SHARED_TEMPLATE)


def test_directives_carry_no_flow_private_references():
    """spore-296: the entity's drift-defense is UNIVERSAL machinery, NOT the identity moat. The flow
    presence-hook variants it generalizes FROM reference flow's own private memory files
    (``continuity.md`` / ``me.md``) — alien to a Levain entity (which has origin/world/partnership).
    A regression re-copying the raw flow variants would inject flow's identity into every sovereign
    entity's context. Word-boundary-anchored so a legitimate ``readme.md``/``frame.md`` mention never
    false-positives on the bare ``me.md`` substring (apparatus L1 note)."""
    joined = "\n".join(DIRECTIVES)
    for token in ("continuity.md", "me.md"):
        assert not re.search(rf"\b{re.escape(token)}", joined, re.IGNORECASE), (
            f"flow-private reference leaked into entity directives: {token!r}"
        )
