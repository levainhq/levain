"""levain.firing.seatbelt_guard — what levain refuses to hand to the macOS sandbox, ever.

A Seatbelt rule over syscalls, Mach traps or MIG routines makes XNU build a syscall mask, and a
malformed mask kernel-panicked a Mac on 2026-10-07 ("mask size exceeds maximum", syscallmask.c).
levain's own profile never contains one (a test pins that); this module is the run-time refusal at
levain's only exec point, so a profile that was altered after rendering still never reaches the
kernel. The token set and the computed-operation forms follow the host guard written the same day
(the embedder's sandbox_guard.py), restated here so levain carries no dependency on flow.

Pure stdlib, no levain imports.
"""
from __future__ import annotations

import re

__all__ = ["scan"]

# Whole-token on the hyphenated-identifier edge, so a path that merely contains the words does not
# match (e.g. ".../xsyscall-unixy").
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_*-])("
    r"syscall(?:\*|-[A-Za-z0-9-]+)?"
    r"|machtrap-number"
    r"|kernel-mig-routine"
    r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# SBPL is TinyScheme: these turn a string into code or pull in unseen text, so an operation name
# could be computed and slip past a literal scan. String literals are blanked first, so a path that
# merely contains one of these words passes.
_SCHEME_SYM = r"A-Za-z0-9_*?!<>=/+.-"
_META_RE = re.compile(
    rf"(?<![{_SCHEME_SYM}])(eval|read|load|open-input-file|open-input-string|string->symbol"
    rf"|string->atom|oblist|the-environment|define-macro|macro|import)(?![{_SCHEME_SYM}])"
)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def scan(profile_text: str) -> list[str]:
    """Every reason to refuse ``profile_text``: kernel-mask tokens (lowercased, first-seen order)
    and computed-operation forms (``eval``, ``load``, ``import`` ...). Empty means it may run."""
    text = profile_text or ""
    hits: list[str] = []
    # A pathname is data, not an operation. Blank string literals for the operation scan, but retain
    # comments: a dangerous token in a comment is refused too, so it cannot be uncommented by a
    # renderer/template mistake without changing what this guard sees.
    token_text = _STRING_RE.sub('""', text)
    for m in _TOKEN_RE.finditer(token_text):
        t = m.group(1).lower()
        if t not in hits:
            hits.append(t)
    # Computed-operation forms are code, so they are looked for in code only: string literals are
    # blanked, then each line is cut at its first ";" (an SBPL comment). The token scan above retains
    # comments but has already blanked strings.
    code = "\n".join(line.split(";", 1)[0] for line in token_text.splitlines())
    for form in _META_RE.findall(code):
        if form not in hits:
            hits.append(form)
    return hits
