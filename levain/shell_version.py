"""The cockpit shell's version derivation: the ONE implementation, pure and standard-library only.

``levain/web_server.py`` calls :func:`derive_shell` at ``make_server`` to write each shell asset's digest into the cockpit
page (``?v=<digest>``) and to derive the service worker's version label; ``scripts/served_bundle_check.py --tree`` loads
THIS file from the tree it checks and calls the same function over that tree's bytes, so the checker recomputes the label
the server would serve instead of copying the formula. Keep it import-free of the rest of ``levain`` (the checker runs it
without the package on its path).

What the label covers: the cockpit page as served, every script and stylesheet the page names (through its ``?v=``
digests), every other path in the server's asset table by its content digest (the mic worklet, which the page loads
without naming it), and the worker's own source. It does NOT cover the web app manifest, the icons, the screenshots or the
signing page: the worker's shell cache holds only the page and the scripts/stylesheets it names, so those files are not in
it and need no reload bar.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping

# Hex characters in an asset digest and in the worker's version label.
SHELL_VERSION_LEN = 12

_SHELL_TAG = re.compile(r"<(?:script|link)\b[^>]*>", re.IGNORECASE)
# The attribute-name boundary: ``data-src`` / ``data-href`` are not the attribute (``\b`` matched them: the hyphen is a word
# boundary). It CONSUMES the character before the name (or the string start) and captures it as group 1, because a regex
# lookbehind is a syntax error in Safari before 16.4 and the same boundary runs in the worker (sw.js ``ATTR_BOUNDARY``, which
# tests/test_shell_asset_versions.py asserts is this text). The rewrite below re-emits group 1.
_ATTR_BOUNDARY = r"(^|[^\w-])"
_SHELL_ATTR = re.compile(_ATTR_BOUNDARY + r"""(src|href)(\s*=\s*)(?:"([^"]+)"|'([^']+)')""", re.IGNORECASE)


def asset_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:SHELL_VERSION_LEN]


def version_shell_page(page: str, digests: Mapping[str, str]) -> str:
    """``page`` with each ``<script src>`` and ``<link href>`` naming a path in ``digests`` (path -> digest) rewritten
    to ``<path>?v=<digest>``. A reference that already carries a query or fragment, or names another path, is left alone."""

    def attr(m: "re.Match[str]") -> str:
        value = m.group(4) if m.group(4) is not None else m.group(5)
        digest = digests.get(value)
        if digest is None:
            return m.group(0)
        quote = '"' if m.group(4) is not None else "'"
        # group 1 is the boundary character the match consumed: it goes back, or the tag loses it
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{quote}{value}?v={digest}{quote}"

    def tag(m: "re.Match[str]") -> str:
        t = m.group(0)
        if t[1:5].lower() == "link" and not re.search(r"""\brel\s*=\s*["']?\s*stylesheet\b""", t, re.IGNORECASE):
            return t
        return _SHELL_ATTR.sub(attr, t, count=1)

    return _SHELL_TAG.sub(tag, page)


def shell_version(versioned_page: bytes, worker_template: bytes, digests: Mapping[str, str]) -> str:
    """The service worker's version label: a digest of the cockpit page AS SERVED (every script and stylesheet it names is
    named by its own content digest, so the page bytes cover the page and every asset it names), of ``digests`` (path ->
    content digest of every other asset in the server's asset table, so an asset the page loads without naming it, the
    mic worklet via ``audioWorklet.addModule``, moves the label too), and of the worker's own source. NOT covered: the
    manifest, icons, screenshots and the signing page (see the module docstring). Content only (no clock, no
    path-dependent order: ``digests`` is sorted): the same bytes give the same label, a one-byte change to the page, any
    covered asset or the worker gives another. Raises ValueError when the page names no versioned shell asset (nothing
    would be covered by the digest)."""
    if not re.search(rb"\?v=[0-9a-f]{%d}[\"']" % SHELL_VERSION_LEN, versioned_page):
        raise ValueError("refusing to start: the cockpit page names no ?v=<digest> shell asset, so the worker's version "
                         "would cover nothing")
    served = "\n".join(f"{p}\t{d}" for p, d in sorted(digests.items())).encode("utf-8")
    return hashlib.sha256(
        b"levain-shell\0" + hashlib.sha256(versioned_page).digest() + hashlib.sha256(served).digest()
        + hashlib.sha256(worker_template).digest()
    ).hexdigest()[:SHELL_VERSION_LEN]


def derive_shell(cockpit_template: str, assets: Mapping[str, bytes], worker_template: bytes,
                 check: Callable[[str, Mapping[str, str]], None] | None = None) -> tuple[bytes, dict[str, str], str]:
    """(the cockpit page as served, path -> digest, the worker's version label) from the cockpit page template, the bytes
    of every OTHER path in the server's asset table (``assets``: path -> bytes) and the worker's source. ``check``, when
    given, is called with the rewritten page and the digests before the label is derived (the server's own bound on a
    shell reference the rewrite did not reach)."""
    digests = {p: asset_digest(b) for p, b in assets.items()}
    page = version_shell_page(cockpit_template, digests)
    if check is not None:
        check(page, digests)
    page_bytes = page.encode("utf-8")
    return page_bytes, digests, shell_version(page_bytes, worker_template, digests)
