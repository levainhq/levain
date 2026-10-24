"""CH-D2 S2, row R15-12-B3: "One test asserts every consumer reads the same parsed representation (no second parse of
the target or the header block)."

Two halves. STRUCTURAL: no module of `levain/chat_egress/` but `parse.py` reads a frame's target or header block or
parses a query (no `urllib.parse`, no `["headers"]` / `["query"]` / `["path"]` read off a frame, no ad-hoc `&` / `;`
split), so a consumer added by any later session can only get these through `parse.parse`'s `ParsedRequest`; this
test runs over whatever modules exist when it runs. RUNTIME: one frame is parsed once, and what strip, override
detection, the backstop, cards and logs read are the fields of that one object.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import levain.chat_egress
from levain.chat_egress import parse

PKG = Path(levain.chat_egress.__file__).resolve().parent
OWNER = PKG / "parse.py"
FRAME_FIELDS = {"headers", "query", "path", "hostHeader"}


def _offences(src: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out += [f"import {a.name}" for a in node.names if a.name.startswith("urllib.parse")]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("urllib.parse"):
            out.append(f"from {node.module} import ...")
        elif isinstance(node, ast.ImportFrom) and node.module == "urllib" and any(
                a.name == "parse" for a in node.names):
            out.append("from urllib import parse")
        elif isinstance(node, ast.Attribute) and node.attr in {"unquote", "unquote_plus", "parse_qs", "parse_qsl",
                                                                "urlsplit", "urlparse"}:
            out.append(f".{node.attr}")
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value in FRAME_FIELDS:
            out.append(f"[{node.slice.value!r}]")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in FRAME_FIELDS:
            out.append(f".get({node.args[0].value!r})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "split" \
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in {"&", ";"}:
            out.append(f".split({node.args[0].value!r})")
    return out


def test_detector_catches_a_second_parse():
    """The detector itself is red on the shapes it exists for (a probe that cannot fail is not evidence)."""
    bad = ("import urllib.parse\nfrom urllib.parse import unquote\n"
           "def f(frame):\n    h = frame['headers']\n    q = frame.get('query')\n    return q.split('&'), h\n")
    assert len(_offences(bad)) == 5


@pytest.mark.parametrize("path", sorted(p for p in PKG.rglob("*.py") if p != OWNER), ids=lambda p: p.name)
def test_r15_12_b3_no_module_but_parse_reads_the_frame(path):
    assert _offences(path.read_text()) == [], f"{path.name} parses the target or header block itself"


def test_r15_12_b3_the_owner_is_the_one_that_parses():
    assert _offences(OWNER.read_text()), "parse.py should be the module holding the parse"


def test_r15_12_b3_one_object_for_every_consumer():
    f = {"t": "req", "id": 7, "method": "GET", "host": "api.example", "port": 443, "sni": "api.example",
         "path": "/r", "query": "%5fmethod=DELETE&access_token=x",
         "headers": {"x_http_method_override": ["DELETE"], "x-original-url": ["/admin"],
                     "x-levain-audit": ["0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"]}}
    p = parse.parse(f)
    # every consumer's read goes through the same object's folded names:
    assert "x-http-method-override" in p.header_names        # override detection (R-7)
    assert "_method" in p.query_keys                          # override detection, the query half
    assert "access_token" in p.query_keys                     # the credential-in-query mark (R-5 / item 3)
    assert p.routing_headers == ("x-original-url",)          # strip (R-1 as amended by R15-13)
    assert parse.walls(p) is None                             # the structural walls read it too
    # the object is a value: re-reading it is free and gives the same answer; nothing re-parses the frame
    f["headers"]["x-original-url"] = ["mutated"]
    assert p.header("x-original-url") == ("/admin",)
