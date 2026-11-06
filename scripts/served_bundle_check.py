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
template), and the worker. The tree is read, never run or imported: the asset table is parsed out of its
``web_server.py`` with ``ast``. Exit 0 all PASS, 1 any FAIL, 2 the origin or the tree could not be read.

Standard library only, so it runs under any interpreter and against any release (an older server with no ``?v=`` digests
FAILs on exactly that). It makes GET requests to the one origin it is given and touches nothing else.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
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
    ok: bool
    name: str
    detail: str = ""

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}" + (f"  {self.detail}" if self.detail else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect is an answer to report, never one to follow
        return None


def fetch(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    """GET ``url`` -> (status, body). A non-2xx answer is a status, not an exception; an unreachable host raises OSError."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:  # noqa: S310 - the operator's origin
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise OSError(f"{url}: {e.reason}") from e


def shell_refs(page: str) -> tuple[dict[str, str], list[str]]:
    """(versioned, bare) from a cockpit page: ``versioned`` is path -> digest for each same-origin script or stylesheet
    named ``<path>?v=<digest>``; ``bare`` lists the same-origin .js/.css paths named without a digest. Other links (icons,
    the manifest) and other origins are not shell assets."""
    versioned: dict[str, str] = {}
    bare: list[str] = []
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
            versioned[path] = v.group(1)
        else:
            bare.append(path)
    return versioned, bare


def package_dir(tree: Path) -> Path:
    """The ``levain`` package directory of ``tree``: a repo checkout or ``git archive`` root (holds ``levain/``) or the
    package itself (a ``site-packages/levain``)."""
    for cand in (tree / "levain", tree):
        if (cand / "templates" / "web").is_dir() and (cand / "web_server.py").is_file():
            return cand
    raise OSError(f"{tree}: no levain package here (looked for levain/templates/web and web_server.py)")


def tree_assets(pkg: Path) -> dict[str, str]:
    """path -> package-data filename, parsed (never imported) from the tree's ``web_server.py`` ``_ASSETS``."""
    for node in ast.parse((pkg / "web_server.py").read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0] if isinstance(node, ast.Assign) else None
        if getattr(target, "id", None) == "_ASSETS" and node.value is not None:
            table = ast.literal_eval(node.value)
            return {p: fn for p, (fn, _ct) in table.items()}
    raise OSError(f"{pkg / 'web_server.py'}: no _ASSETS table found")


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
    versioned, bare = shell_refs(page)
    if not versioned:
        out.append(Result(False, "page", "names no ?v=<digest> shell asset: this server predates the content-hash shell (row 35)"))
    for path in bare:
        out.append(Result(False, f"page {path}", "named without its ?v= digest"))
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


def check_tree(base: str, pkg: Path, page: str, versioned: dict[str, str], sw: bytes,
               get: Callable[[str], tuple[int, bytes]] = fetch) -> list[Result]:
    """The origin against a tree's bytes."""
    out: list[Result] = []
    assets = tree_assets(pkg)
    web = pkg / "templates" / "web"
    # the cockpit page: the server only appends ?v=<digest> to the template's shell refs, so with those taken off both
    # sides the served page IS the template
    template = (web / assets["/"]).read_text(encoding="utf-8") if "/" in assets else ""
    if _VERSION_QUERY.sub("", page) == _VERSION_QUERY.sub("", template):
        out.append(Result(True, "tree /", "served page (minus ?v=) equals the tree's template"))
    else:
        out.append(Result(False, "tree /", "served page (minus ?v=) differs from the tree's dashboard template"))
    for path in sorted(versioned):
        fn = assets.get(path)
        if fn is None or not (web / fn).is_file():
            out.append(Result(False, f"tree {path}", "the tree has no such shell asset"))
            continue
        want = digest((web / fn).read_bytes())
        out.append(Result(versioned[path] == want, f"tree {path}",
                          f"served ?v={versioned[path]}, tree bytes hash to {want}" if versioned[path] != want else f"?v={want}"))
    # the tree's own shell refs (bare in the template) that the served page does not name
    t_paths = {p for p in _template_shell_paths(template) if p in assets}
    for path in sorted(t_paths - set(versioned)):
        out.append(Result(False, f"tree {path}", "the tree's page names it; the served page does not"))
    # every other static page and script the tree serves, read bare
    for path, fn in sorted(assets.items()):
        if path == "/" or path in versioned or not (web / fn).is_file():
            continue
        status, data = get(base + path)
        want = digest((web / fn).read_bytes())
        if status != 200:
            out.append(Result(False, f"tree {path}", f"HTTP {status}"))
        else:
            got = digest(data)
            out.append(Result(got == want, f"tree {path}",
                              f"served bytes hash to {got}, tree bytes hash to {want}" if got != want else got))
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
    versioned, bare = shell_refs(template)
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
    print(f"{len(results) - len(failed)} PASS, {len(failed)} FAIL" + (": " + ", ".join(r.name for r in failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
