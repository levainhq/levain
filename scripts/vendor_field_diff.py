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
A field the vendor removed stays on a codec's list (old clients still send it); this script never reports a removal.
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


def schema_fields(doc: Mapping[str, Any], name: str) -> Set[str]:
    """The property names of `components.schemas.<name>`, following `allOf` and `$ref` (an object schema)."""
    schemas = doc["components"]["schemas"]
    out: Set[str] = set()
    seen: Set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            key = ref.rsplit("/", 1)[-1]
            if key not in seen and key in schemas:
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
    """The annotated names of class `cls` in a Python source (the SDK's request TypedDict)."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    raise KeyError(cls)


#: fields every SDK adds on top of the TypedDict base (`stream` is a Literal overload, not a base field)
_ANTHROPIC_EXTRA = {"stream"}


def vendor_fields(source: str, raw: bytes) -> Set[str]:
    fname, name, _ = SOURCES[source]
    if fname.endswith(".py"):
        return typeddict_fields(raw.decode("utf-8"), name) | _ANTHROPIC_EXTRA
    if fname.endswith(".yaml"):
        import yaml   # a dev dependency of this script only
        return schema_fields(yaml.safe_load(raw), name)
    return schema_fields(json.loads(raw), name)


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
        if fname not in cache:
            if args.snapshots:
                with open("%s/%s" % (args.snapshots.rstrip("/"), fname), "rb") as f:
                    cache[fname] = f.read()
            else:
                cache[fname] = _fetch(_url(source))
        found = untriaged(source, cache[fname])
        total += len(found)
        print("%s (vs %s): %s" % (source, codec, ", ".join(found) if found else "nothing untriaged"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
