#!/usr/bin/env python3
"""Release-time diff: the request fields each vendor publishes that a codec has not triaged.

A dev script, not shipped, and the only part of levain that reads the network. For each (vendor schema, codec) pair
it prints the top-level request fields the vendor's schema lists that are on none of the codec's three lists:
`dispatch_fields` (judged bounded), `unbounded_fields` (judged unbounded, `body-unbounded` on dispatch) and
`unjudged_fields` (seen, not yet measured, refused on dispatch). Each printed field is triaged before the release:
bounded goes into `dispatch_fields`; unbounded goes into `unbounded_fields` with a `_DISABLED_BY` row; unknown goes
into `unjudged_fields` and gets a measured call. The release checklist reads "the diff printed nothing untriaged".

    python3 scripts/vendor_field_diff.py                 # fetch every source and diff
    python3 scripts/vendor_field_diff.py --snapshots D   # read D/<source>.<ext> instead of the network

Sources (Anthropic is pinned to a tag; bump `ANTHROPIC_SDK_TAG` per release, never `main`):
  openai-chat, openai-responses   github.com/openai/openai-openapi  openapi.yaml
  openrouter-*                    openrouter.ai/openapi.json
  anthropic-messages              anthropic-sdk-python `types/beta/message_create_params.py` TypedDict
  anthropic-betas                 anthropic-sdk-python `types/anthropic_beta_param.py` (the beta-name literal), plus the
                                  `anthropic-beta` values a real client sent (CAPTURED_BETAS, read from the repo)
A field the vendor removed stays on a codec's list (old clients still send it); this script never reports a removal.
The beta section prints each beta name the SDK literal or the captured headers carry that is on none of the codec's
beta lists (`dispatch_betas`, `unbounded_betas`, `unjudged_betas`, `other_route_betas`). Before a release, every
printed beta is judged and moved onto one of the four lists.

Exit status: 0 nothing untriaged, 1 untriaged fields found, 2 the check could not run (a fetch failure, a missing or
unparseable snapshot, a request schema or class not found, an unresolved `$ref`), with one line on stderr saying why.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

ANTHROPIC_SDK_TAG = "v2.7.0"
ANTHROPIC_SDK_URL = ("https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/%s/src/anthropic/types/beta/"
                     "message_create_params.py")
ANTHROPIC_BETA_URL = ("https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/%s/src/anthropic/types/"
                      "anthropic_beta_param.py")
#: the snapshot name of the beta-name literal, and the codec and fixture the beta section is diffed against
BETA_SNAPSHOT = "anthropic_beta_param.py"
BETA_CODEC = "anthropic-messages"
CAPTURED_BETAS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
                              "client_bodies", "claude_code_anthropic_beta_headers.json")
OPENAI_URL = "https://raw.githubusercontent.com/openai/openai-openapi/main/openapi.yaml"
OPENROUTER_URL = "https://openrouter.ai/openapi.json"

#: source name -> (file the snapshot is read from, how the fields are read, the codec it is diffed against)
SOURCES: Dict[str, Tuple[str, str, str]] = {
    "openai-chat": ("openai.yaml", "CreateChatCompletionRequest", "openai-chat"),
    "openai-responses": ("openai.yaml", "CreateResponse", "openai-responses"),
    "openrouter-chat": ("openrouter.json", "ChatRequest", "openai-chat"),
    "openrouter-responses": ("openrouter.json", "ResponsesRequest", "openai-responses"),
    "openrouter-messages": ("openrouter.json", "MessagesRequest", "anthropic-messages"),
    "anthropic-messages": ("anthropic_message_create_params.py", "MessageCreateParamsBase", "anthropic-messages"),
}


class CheckFailed(Exception):
    """The diff could not be computed: its message is the one line `main` prints."""


def schema_fields(doc: Mapping[str, Any], name: str) -> Set[str]:
    """The property names of `components.schemas.<name>`, following `allOf` and `$ref` (an object schema). A `$ref`
    that names no schema raises `CheckFailed`: a field behind it would be silently missed."""
    try:
        schemas = doc["components"]["schemas"]
    except (KeyError, TypeError):
        raise CheckFailed("no components.schemas in the schema document") from None
    if not isinstance(schemas, dict) or name not in schemas:
        raise CheckFailed("schema %s not found" % name)
    out: Set[str] = set()
    seen: Set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            key = ref.rsplit("/", 1)[-1]
            if key not in schemas:
                raise CheckFailed("unresolved $ref %s (under %s)" % (ref, name))
            if key not in seen:
                seen.add(key)
                walk(schemas[key])
            # no return: OpenAPI 3.1 lets a `$ref` carry sibling `properties` / `allOf`
        props = node.get("properties")
        if isinstance(props, dict):
            out.update(props)
        for part in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(part, ()) or ():
                walk(sub)

    walk(schemas[name])
    return out


def typeddict_fields(source: str, cls: str) -> Set[str]:
    """The annotated names of class `cls` in a Python source (the SDK's request TypedDict), with those of every base
    class defined in the same source, recursively: a field the SDK moves into a base is still a field."""
    classes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
    if cls not in classes:
        raise CheckFailed("class %s not found" % cls)
    out: Set[str] = set()
    seen: Set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        node = classes[name]
        out.update(n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name))
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                walk(base.id)

    walk(cls)
    return out


#: fields every SDK adds on top of the TypedDict base (`stream` is a Literal overload, not a base field)
_ANTHROPIC_EXTRA = {"stream"}


def vendor_fields(source: str, raw: bytes) -> Set[str]:
    """The vendor's request fields for `source`; a snapshot that does not parse raises `CheckFailed`."""
    fname, name, _ = SOURCES[source]
    if fname.endswith(".py"):
        try:
            text = raw.decode("utf-8")
            return typeddict_fields(text, name) | _ANTHROPIC_EXTRA
        except (SyntaxError, ValueError) as e:
            raise CheckFailed("%s did not parse (%s)" % (fname, type(e).__name__)) from None
    if fname.endswith(".yaml"):
        import yaml   # a dev dependency of this script only
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise CheckFailed("%s did not parse (%s)" % (fname, type(e).__name__)) from None
    else:
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise CheckFailed("%s did not parse (%s)" % (fname, type(e).__name__)) from None
    return schema_fields(doc, name)


def untriaged(source: str, raw: bytes, codecs: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The vendor's fields for `source` that its codec lists in none of `dispatch_fields`, `unbounded_fields`,
    `unjudged_fields`."""
    if codecs is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repo root
        from levain.broker.codec import CODECS
        codecs = CODECS
    codec = codecs[SOURCES[source][2]]
    known = set(codec.dispatch_fields) | set(codec.unbounded_fields) | set(codec.unjudged_fields)
    return sorted(vendor_fields(source, raw) - known)


def beta_names(raw: bytes) -> Set[str]:
    """The beta names of the SDK's `AnthropicBetaParam`: the elements of each `Literal[...]` subscript in it, and no
    other string (an `Annotated` note is not a name). A snapshot that does not parse, holds no such alias or no
    `Literal`, or a `Literal` element that is not a string, raises `CheckFailed`."""
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, ValueError) as e:
        raise CheckFailed("%s did not parse (%s)" % (BETA_SNAPSHOT, type(e).__name__)) from None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "AnthropicBetaParam" and node.value is not None:
            out: Set[str] = set()
            for n in ast.walk(node.value):
                if not (isinstance(n, ast.Subscript) and (
                        (isinstance(n.value, ast.Name) and n.value.id == "Literal")
                        or (isinstance(n.value, ast.Attribute) and n.value.attr == "Literal"))):
                    continue
                elts = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
                for e in elts:
                    if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
                        raise CheckFailed("AnthropicBetaParam in %s has a Literal element that is not a string"
                                          % BETA_SNAPSHOT)
                    out.add(e.value)
            if out:
                return out
    raise CheckFailed("AnthropicBetaParam not found in %s" % BETA_SNAPSHOT)


def captured_betas(raw: bytes) -> Set[str]:
    """The beta names in the captured `anthropic-beta` header values (`{"requests": [<value>, ...]}`), split on commas
    and stripped of spaces and tabs, as `Codec.check_betas` reads them; an empty token (a trailing comma) is not a
    name."""
    try:
        doc = json.loads(raw)
        values = doc["requests"]
        return {t.strip(" \t") for v in values for t in v.split(",") if t.strip(" \t")}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CheckFailed("the captured anthropic-beta headers did not parse") from None


def untriaged_betas(snapshot: bytes, captured: bytes, codecs: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The beta names in the SDK literal or the captured headers that the codec lists in none of `dispatch_betas`,
    `unbounded_betas`, `unjudged_betas`, `other_route_betas`. A removal is never reported."""
    if codecs is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repo root
        from levain.broker.codec import CODECS
        codecs = CODECS
    c = codecs[BETA_CODEC]
    known = set(c.dispatch_betas) | set(c.unbounded_betas) | set(c.unjudged_betas) | set(c.other_route_betas)
    return sorted((beta_names(snapshot) | captured_betas(captured)) - known)


def _url(source: str) -> str:
    fname = SOURCES[source][0]
    return OPENAI_URL if fname == "openai.yaml" else OPENROUTER_URL if fname == "openrouter.json" else \
        ANTHROPIC_SDK_URL % ANTHROPIC_SDK_TAG


def _fetch(url: str) -> bytes:
    # a named agent: openrouter.ai answers the default urllib one 403
    req = urllib.request.Request(url, headers={"User-Agent": "levain-vendor-field-diff/1"})
    with urllib.request.urlopen(req, timeout=60) as r:   # noqa: S310 the fixed https URLs above
        return r.read()


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshots", help="a directory holding each source file by its snapshot name, instead of the network")
    args = ap.parse_args(list(argv) if argv is not None else None)
    cache: Dict[str, bytes] = {}
    total = 0
    for source, (fname, _name, codec) in SOURCES.items():
        try:
            if fname not in cache:
                if args.snapshots:
                    path = "%s/%s" % (args.snapshots.rstrip("/"), fname)
                    try:
                        with open(path, "rb") as f:
                            cache[fname] = f.read()
                    except OSError as e:
                        raise CheckFailed("snapshot %s not read (%s)" % (path, e.strerror or type(e).__name__))
                else:
                    url = _url(source)
                    try:
                        cache[fname] = _fetch(url)
                    except OSError as e:   # urllib's URLError and HTTPError are OSErrors
                        raise CheckFailed("fetch of %s failed (%s)" % (url, e))
            found = untriaged(source, cache[fname])
        except CheckFailed as e:
            print("vendor_field_diff: could not run: %s: %s" % (source, e), file=sys.stderr)
            return 2
        total += len(found)
        print("%s (vs %s): %s" % (source, codec, ", ".join(found) if found else "nothing untriaged"))
    try:
        if args.snapshots:
            path = "%s/%s" % (args.snapshots.rstrip("/"), BETA_SNAPSHOT)
            try:
                with open(path, "rb") as f:
                    snap = f.read()
            except OSError as e:
                raise CheckFailed("snapshot %s not read (%s)" % (path, e.strerror or type(e).__name__))
        else:
            url = ANTHROPIC_BETA_URL % ANTHROPIC_SDK_TAG
            try:
                snap = _fetch(url)
            except OSError as e:
                raise CheckFailed("fetch of %s failed (%s)" % (url, e))
        try:
            with open(CAPTURED_BETAS, "rb") as f:
                cap = f.read()
        except OSError as e:
            raise CheckFailed("captured betas %s not read (%s)" % (CAPTURED_BETAS, e.strerror or type(e).__name__))
        found = untriaged_betas(snap, cap)
    except CheckFailed as e:
        print("vendor_field_diff: could not run: anthropic-betas: %s" % e, file=sys.stderr)
        return 2
    total += len(found)
    print("anthropic-betas (vs %s): %s" % (BETA_CODEC, ", ".join(found) if found else "nothing untriaged"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
