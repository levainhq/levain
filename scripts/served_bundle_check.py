#!/usr/bin/env python3
"""Served-bundle checker: "merged is not running" as one command.

A fix on main is not running until a release carrying it is installed in the server the operator's device talks to. The
hand recipe was ``curl /sw.js | grep`` and ``diff -rq <installed>/levain <tree>/levain``; this does the same reading and
prints PASS or FAIL per item.

    served_bundle_check.py --url https://host.tailnet.ts.net [--tree <repo checkout | site-packages/levain>]

Without ``--tree`` it asks only whether the origin is self-consistent: every ``<script src>`` and ``<link
rel=stylesheet>`` the cockpit page names carries ``?v=<digest>``, the bytes the server serves for that URL hash to that
digest, and the served worker (``/sw.js``) has the shape the content-hash redesign (STATUS row 35) gave it.

With ``--tree`` it also asks whether the origin serves the BYTES of that tree: each shell asset's digest, every other
static page and script, the cockpit page itself (the served page minus its ``?v=`` digests must equal the tree's
template), the sign sheet (``/sign/<id>``, served only beside a signer: an origin without one prints UNVERIFIED for it
and the last line says so), and the worker. The tree is read as the server reads it (``read_text``, so a CRLF checkout
matches), never run or imported: the asset table is parsed out of its ``web_server.py`` with ``ast``. Exit 0 no FAIL (an
UNVERIFIED line is not a FAIL, and is never counted as a PASS), 1 any FAIL, 2 the origin or the tree could not be read (a
declared asset the tree lacks, a cut, oversized or unfinished-in-time response, a malformed asset table, a tree file that is not UTF-8).

Standard library only, so it runs under any interpreter and against any release (an older server with no ``?v=`` digests
FAILs on exactly that). It makes GET requests to the one origin it is given and touches nothing else.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DIGEST_LEN = 12  # levain/web_server.py ``_asset_digest``: sha256 hex, first 12
_DIGEST = re.compile(r"[0-9a-f]{%d}" % DIGEST_LEN)
_TAG = re.compile(r"<(?:script|link)\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""(?:^|[^\w-])(src|href|rel)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_VERSION_QUERY = re.compile(r"\?v=[0-9a-f]{%d}(?=[\"'])" % DIGEST_LEN)
_SHELL_EXT = (".js", ".css")
_SHEET_PROBE_ID = "served-bundle-check"   # any sign request id (8-64 of [A-Za-z0-9_-]): the sheet is the same bytes for every id

# The worker's row 35 shape (the content-hash redesign): the attribute boundary shared with the server, a cache-first
# answer for a versioned URL, a cache name that carries the worker's own version. ``navOffline`` is the worker-wide flag
# row 35 deleted. A rename in the worker turns this red against the tree in tests/test_served_bundle_check.py.
SW_REQUIRED = ("const ATTR_BOUNDARY = String.raw", "async function cachedAsset(", "'levain-shell-v' + SW_VERSION")
SW_FORBIDDEN = ("navOffline",)
_SW_VERSION = re.compile(r"""const\s+SW_VERSION\s*=\s*['"]([^'"]+)['"]""")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:DIGEST_LEN]


@dataclass(frozen=True)
class Result:
    """One line of the report. ``unverified`` is a PASS-shaped line that did NOT compare anything (``ok`` is True so it
    turns no exit code red), printed as UNVERIFIED and counted apart so the verdict never reads all-PASS without it."""
    ok: bool
    name: str
    detail: str = ""
    unverified: bool = False

    def line(self) -> str:
        word = "UNVERIFIED" if self.unverified else "PASS" if self.ok else "FAIL"
        return f"{word}  {self.name}" + (f"  {self.detail}" if self.detail else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect is an answer to report, never one to follow
        return None


MAX_BODY = 16 * 1024 * 1024   # the largest response read: the cockpit's biggest asset is a small fraction of this
_CHUNK = 64 * 1024


def _socket_of(r):
    """The socket under open response ``r`` (an ``HTTPResponse``, or the ``HTTPError`` that wraps one), or None."""
    x = r
    for _ in range(4):
        raw = getattr(x, "raw", None)
        if raw is not None:
            return getattr(raw, "_sock", None)
        x = getattr(x, "fp", None)
        if x is None:
            return None
    return None


def _read_bounded(url: str, r, deadline: float) -> bytes:
    """The body of open response ``r``: at most ``MAX_BODY`` bytes, finished before the absolute monotonic ``deadline``
    (the socket timeout alone resets on every received byte), and as long as its Content-Length says (a connection that
    closes early is a cut body, which ``read(amt)`` returns as a short read rather than raising). Each blocking read is
    given only the time left, so a stalled stream is given up AT the deadline; the deadline is looked at again after every
    read, the one that returns nothing included, so a read that ends after it is over-deadline, never a PASS."""
    parts: list[bytes] = []
    total = 0
    sock = _socket_of(r)

    def over() -> OSError:
        return OSError(f"{url}: not finished before the total deadline ({total} bytes read)")

    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise over()
        if sock is not None:
            try:
                sock.settimeout(left)
            except OSError:                   # the body is complete and the response closed its socket: nothing can block now
                sock = None
        try:
            chunk = r.read1(_CHUNK)       # ONE socket read, so the deadline is looked at after every packet, not every 64 KiB
        except TimeoutError as e:
            raise over() from e
        if time.monotonic() > deadline:
            raise over()
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BODY:
            raise OSError(f"{url}: response larger than {MAX_BODY} bytes")
        parts.append(chunk)
    want = r.headers.get("Content-Length")
    if want is not None and want.isdigit() and int(want) != total:
        raise OSError(f"{url}: body cut short ({total} of {want} bytes)")
    return b"".join(parts)


def fetch(url: str, timeout: float = 8.0, total_timeout: float | None = None) -> tuple[int, bytes]:
    """GET ``url`` -> (status, body). A non-2xx answer is a status, not an exception; an unreachable host, a cut or
    oversized body, a malformed answer and a read past ``total_timeout`` (default three times ``timeout``) raise OSError."""
    deadline = time.monotonic() + (total_timeout if total_timeout is not None else timeout * 3)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        try:
            with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:  # noqa: S310 - the operator's origin
                return r.status, _read_bounded(url, r, deadline)
        except urllib.error.HTTPError as e:
            return e.code, _read_bounded(url, e, deadline)
    except urllib.error.URLError as e:
        raise OSError(f"{url}: {e.reason}") from e
    except http.client.HTTPException as e:       # IncompleteRead, BadStatusLine, InvalidURL: not OSErrors
        raise OSError(f"{url}: {type(e).__name__}: {e}") from e


def shell_refs(page: str) -> tuple[dict[str, str], list[str], dict[str, list[str]]]:
    """(versioned, bare, conflicts) from a cockpit page: ``versioned`` is path -> digest for each same-origin script or
    stylesheet named ``<path>?v=<digest>``; ``bare`` lists the same-origin .js/.css paths named without a digest;
    ``conflicts`` maps a path the page names with two or more different digests to those digests (``versioned`` keeps the
    first). Other links (icons, the manifest) and other origins are not shell assets."""
    versioned: dict[str, str] = {}
    bare: list[str] = []
    conflicts: dict[str, list[str]] = {}
    for m in _TAG.finditer(page):
        tag = m.group(0)
        attrs = {a.group(1).lower(): (a.group(2) if a.group(2) is not None else a.group(3)) for a in _ATTR.finditer(tag)}
        if tag[1:5].lower() == "link":
            if "stylesheet" not in (attrs.get("rel") or "").lower().split():
                continue
            ref = attrs.get("href")
        else:
            ref = attrs.get("src")
        if not ref or not ref.startswith("/") or ref.startswith("//"):
            continue
        path, _, query = ref.partition("?")
        if not path.endswith(_SHELL_EXT):
            continue
        v = re.fullmatch(r"v=(%s)" % _DIGEST.pattern, query)
        if v:
            first = versioned.setdefault(path, v.group(1))
            if first != v.group(1) and v.group(1) not in conflicts.setdefault(path, [first]):
                conflicts[path].append(v.group(1))
        elif path not in bare:
            bare.append(path)
    return versioned, bare, conflicts


def package_dir(tree: Path) -> Path:
    """The ``levain`` package directory of ``tree``: a repo checkout or ``git archive`` root (holds ``levain/``) or the
    package itself (a ``site-packages/levain``)."""
    for cand in (tree / "levain", tree):
        if (cand / "templates" / "web").is_dir() and (cand / "web_server.py").is_file():
            return cand
    raise OSError(f"{tree}: no levain package here (looked for levain/templates/web and web_server.py)")


def _tree_literal(pkg: Path, name: str):
    """The literal value of the module-level ``name`` in the tree's ``web_server.py``, parsed (never imported)."""
    for node in ast.parse((pkg / "web_server.py").read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0] if isinstance(node, ast.Assign) else None
        if getattr(target, "id", None) == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise OSError(f"{pkg / 'web_server.py'}: no {name} found")


def tree_assets(pkg: Path) -> dict[str, str]:
    """path -> package-data filename, parsed from the tree's ``_ASSETS``. Every file it names must exist in
    ``templates/web``: a table that names a file the tree lacks is an unreadable tree (OSError), never a silent skip."""
    table = _tree_literal(pkg, "_ASSETS")
    where = pkg / "web_server.py"
    if not isinstance(table, dict):
        raise OSError(f"{where}: _ASSETS is not a path -> (file, content type) table")
    out: dict[str, str] = {}
    for path, row in table.items():
        if not (isinstance(path, str) and isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], str)):
            raise OSError(f"{where}: _ASSETS row {path!r} is not a (file, content type) pair")
        if not (pkg / "templates" / "web" / row[0]).is_file():
            raise OSError(f"{pkg / 'templates' / 'web' / row[0]}: _ASSETS names it for {path} and the tree does not have it")
        out[path] = row[0]
    return out


def tree_sheet(pkg: Path) -> tuple[str, str]:
    """(URL prefix, filename) of the sign sheet, parsed from the tree's ``_SHEET_PREFIX`` and ``_SHEET_PAGE``. The sheet is
    loaded outside ``_ASSETS`` and served at ``<prefix><request id>`` only beside a signer."""
    prefix, name = _tree_literal(pkg, "_SHEET_PREFIX"), _tree_literal(pkg, "_SHEET_PAGE")
    if not (isinstance(prefix, str) and isinstance(name, str)):
        raise OSError(f"{pkg / 'web_server.py'}: _SHEET_PREFIX / _SHEET_PAGE are not strings")
    if not (pkg / "templates" / "web" / name).is_file():
        raise OSError(f"{pkg / 'templates' / 'web' / name}: _SHEET_PAGE names it and the tree does not have it")
    return prefix, name


def tree_bytes(path: Path) -> bytes:
    """A tree file's bytes as the server serves them: ``load_web_asset`` is ``read_text(encoding="utf-8")`` (universal
    newlines, so a CRLF checkout reads as LF) and the server ``.encode("utf-8")``s that, so read it the same way."""
    try:
        return path.read_text(encoding="utf-8").encode("utf-8")
    except UnicodeDecodeError as e:          # the server could not serve it either: an unreadable tree, not a traceback
        raise OSError(f"{path}: not valid UTF-8 ({e.reason} at byte {e.start})") from e


def _sw_version(sw: bytes) -> str:
    m = _SW_VERSION.search(sw.decode("utf-8", "replace"))
    return m.group(1) if m else "?"


def check_origin(base: str, get: Callable[[str], tuple[int, bytes]] = fetch) -> tuple[list[Result], str, dict[str, str], bytes]:
    """The origin against itself. Returns (results, page text, versioned refs, sw bytes)."""
    out: list[Result] = []
    status, body = get(base + "/")
    if status != 200:
        raise OSError(f"{base}/: HTTP {status} (the cockpit page is a token-free shell; is this the cockpit origin?)")
    page = body.decode("utf-8", "replace")
    versioned, bare, conflicts = shell_refs(page)
    if not versioned:
        out.append(Result(False, "page", "names no ?v=<digest> shell asset: this server predates the content-hash shell (row 35)"))
    why_page: dict[str, list[str]] = {}       # ONE result per path, carrying every reason the page is wrong about it
    for path in bare:
        why_page.setdefault(path, []).append("named without its ?v= digest")
    for path, digests in conflicts.items():
        why_page.setdefault(path, []).append("named with different ?v= digests: " + ", ".join(digests))
    for path in sorted(why_page):
        out.append(Result(False, f"page {path}", "; ".join(why_page[path])))
    for path, want in sorted(versioned.items()):
        status, data = get(f"{base}{path}?v={want}")
        got = digest(data) if status == 200 else None
        if status != 200:
            out.append(Result(False, f"served {path}", f"HTTP {status} for the URL the page names (?v={want})"))
        elif got != want:
            out.append(Result(False, f"served {path}", f"page names ?v={want}, bytes served hash to {got}"))
        else:
            out.append(Result(True, f"served {path}", f"?v={want}"))
    status, sw = get(base + "/sw.js")
    if status != 200:
        out.append(Result(False, "worker /sw.js", f"HTTP {status}"))
        sw = b""
    else:
        text = sw.decode("utf-8", "replace")
        missing = [m for m in SW_REQUIRED if m not in text]
        present = [m for m in SW_FORBIDDEN if m in text]
        ver = _sw_version(sw)
        if missing or present:
            why = ([f"lacks {m!r}" for m in missing] + [f"still has {m!r}" for m in present])
            out.append(Result(False, "worker shape", f"SW_VERSION {ver}: not the row 35 worker: " + "; ".join(why)))
        else:
            out.append(Result(True, "worker shape", f"SW_VERSION {ver}: row 35 shape"))
    return out, page, versioned, sw


def _compare_bare(base: str, path: str, want_bytes: bytes, get: Callable[[str], tuple[int, bytes]]) -> Result:
    """One path read with no query and compared with the tree's bytes."""
    status, data = get(base + path)
    if status != 200:
        return Result(False, f"tree {path}", f"HTTP {status}")
    got, want = digest(data), digest(want_bytes)
    return Result(got == want, f"tree {path}", f"served bytes hash to {got}, tree bytes hash to {want}" if got != want else got)


def check_tree(base: str, pkg: Path, page: str, versioned: dict[str, str], sw: bytes,
               get: Callable[[str], tuple[int, bytes]] = fetch) -> list[Result]:
    """The origin against a tree's bytes. Every path gets ONE ``tree <path>`` result."""
    out: list[Result] = []
    assets = tree_assets(pkg)
    sheet_prefix, sheet_file = tree_sheet(pkg)
    web = pkg / "templates" / "web"
    # the cockpit page: the server only appends ?v=<digest> to the template's shell refs, so with those taken off both
    # sides the served page IS the template
    template = tree_bytes(web / assets["/"]).decode("utf-8") if "/" in assets else ""
    if _VERSION_QUERY.sub("", page) == _VERSION_QUERY.sub("", template):
        out.append(Result(True, "tree /", "served page (minus ?v=) equals the tree's template"))
    else:
        out.append(Result(False, "tree /", "served page (minus ?v=) differs from the tree's dashboard template"))
    for path in sorted(versioned):
        fn = assets.get(path)
        if fn is None:
            out.append(Result(False, f"tree {path}", "the tree has no such shell asset"))
            continue
        want = digest(tree_bytes(web / fn))
        out.append(Result(versioned[path] == want, f"tree {path}",
                          f"served ?v={versioned[path]}, tree bytes hash to {want}" if versioned[path] != want else f"?v={want}"))
    # the tree's own shell refs (bare in the template). One the served page names bare is read bare and judged on its
    # bytes (check_origin has already failed it for the missing digest); one the served page does not name is a FAIL
    t_paths = {p for p in _template_shell_paths(template) if p in assets}
    served_bare = set(shell_refs(page)[1])
    for path in sorted(t_paths - set(versioned)):
        if path in served_bare:
            out.append(_compare_bare(base, path, tree_bytes(web / assets[path]), get))
        else:
            out.append(Result(False, f"tree {path}", "the tree's page names it; the served page does not"))
    # every other static page and script the tree serves, read bare
    for path, fn in sorted(assets.items()):
        if path == "/" or path in versioned or path in t_paths:
            continue
        out.append(_compare_bare(base, path, tree_bytes(web / fn), get))
    # the sign sheet is loaded outside _ASSETS and served only beside a signer: compared when served, else named UNVERIFIED
    status, data = get(f"{base}{sheet_prefix}{_SHEET_PROBE_ID}")
    served_at = f"{sheet_prefix}<id>"
    if status == 200:
        want, got = digest(tree_bytes(web / sheet_file)), digest(data)
        out.append(Result(got == want, "tree sign sheet", f"served bytes hash to {got}, tree bytes hash to {want}" if got != want else got))
    elif status == 404:
        out.append(Result(True, f"sign sheet (served at {served_at})",
                          "this origin has no signer, so the sheet is not served and was not compared", unverified=True))
    else:
        out.append(Result(False, "tree sign sheet", f"HTTP {status} at {served_at}"))
    t_sw = pkg / "webpush" / "static" / "sw.js"
    if t_sw.is_file() and sw:
        tb = t_sw.read_bytes()
        same = tb == sw
        out.append(Result(same, "tree /sw.js", f"SW_VERSION {_sw_version(sw)}" if same else
                          f"served SW_VERSION {_sw_version(sw)} ({digest(sw)}), tree SW_VERSION {_sw_version(tb)} ({digest(tb)})"))
    elif not t_sw.is_file():
        out.append(Result(False, "tree /sw.js", "the tree has no webpush/static/sw.js"))
    return out


def _template_shell_paths(template: str) -> set[str]:
    versioned, bare, _ = shell_refs(template)
    return set(versioned) | set(bare)


def run(base: str, tree: Path | None, get: Callable[[str], tuple[int, bytes]] = fetch) -> list[Result]:
    base = base.rstrip("/")
    pkg = package_dir(tree) if tree is not None else None
    results, page, versioned, sw = check_origin(base, get)
    if pkg is not None:
        results += check_tree(base, pkg, page, versioned, sw, get)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the cockpit origin, e.g. https://host.tailnet.ts.net or http://127.0.0.1:8765")
    ap.add_argument("--tree", type=Path, help="a levain checkout / git archive root, or an installed site-packages/levain")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args(argv)
    try:
        results = run(args.url, args.tree, lambda u: fetch(u, args.timeout))
    except (OSError, SyntaxError, ValueError) as e:
        print(f"ERROR  {e}", file=sys.stderr)
        return 2
    for r in results:
        print(r.line())
    failed = [r for r in results if not r.ok]
    unverified = [r for r in results if r.unverified]
    passed = len(results) - len(failed) - len(unverified)
    print(f"{passed} PASS, {len(failed)} FAIL" + (": " + ", ".join(r.name for r in failed) if failed else "")
          + (f", {len(unverified)} UNVERIFIED: " + ", ".join(r.name.split(" (")[0] for r in unverified) if unverified else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
