"""The body codecs of a metered brokered route (3c R4 §1.3, §1.6): one class per `body_codec` of `policy.BODY_CODECS`
that can be metered (every one but `none`). keys runs them on the buffered body in §1.7 step 4-5's order:
`parse` (one JSON parse), `check` (the model, the ticket, the unboundable shapes), `write_ceiling` (openai-* only),
`estimate` (the admitted hold). After the relay, `usage` (R4 §1.7 step 8) reads the response on the side and
`price_usage` prices what it reported, by class, at the same entry and the same rounding as the estimate.

Every refusal is a `norm.Refused`. Any `RecursionError` in these steps is `body-json` (R4 §10), raised by `parse`
for the parse and by `guarded` for the walks after it, so no hold is taken for a body keys cannot walk.

Runs on the root interpreter (3.9): no `match`, no 3.10+ API.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, TypeVar

from levain.broker import norm, policy
from levain.broker.policy import Price

T = TypeVar("T")

#: a tool type's version suffix, the name `per_use` keys a priced tool by: Anthropic's `_YYYYMMDD`
#: (`web_search_20250305`) and OpenAI's snapshot `_YYYY_MM_DD` (`web_search_2025_08_26`).
_TOOL_VERSION = re.compile(r"_(?:\d{8}|\d{4}_\d{2}_\d{2})\Z")


def _tool_name(t: Any) -> Optional[str]:
    """A `tools[]` entry's type with its version suffix removed; None for an entry with no string `type`. The one
    normaliser `_tools` (the unbounded check) and `_uses` (the per-use price) both read."""
    if isinstance(t, dict) and isinstance(t.get("type"), str):
        return _TOOL_VERSION.sub("", t["type"])
    return None


#: what DISABLES each unbounded field: the field is unbounded unless it is absent or holds one of these values,
#: compared by identity / exact type, so `0` and `{}` are never "absent". Read from each provider's reference
#: [judged by lane 3c2a-i-fix1, 2026-10-23, against the pages named in 1023-8/3c2a/A1_FIX1_RETURN.md]:
#:   `web_search_options` (OpenAI Chat): presence enables search, `{}` included; nothing but absence disables it.
#:   `plugins` (OpenRouter) and `mcp_servers` (Anthropic): a list of things to enable; `[]` enables none.
#:   `previous_response_id` and `conversation` (OpenAI Responses): `string or null`; `null` is no prior state.
#:   `prompt` (OpenAI Responses): a stored prompt template held by the provider; `null` is none.
#:   `max_tool_calls` (OpenAI, OpenRouter): how many server-side tool calls the provider may run; `null` is unset.
#:   `compaction` (Anthropic, on-demand): the summary call's billed tokens are only in `usage.iterations` (the top
#:   level reads 0), https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand; `null` is none.
#:   [read by lane prel3fix3r1, 2026-11-08]
_DISABLED_BY = {
    "web_search_options": (),
    "plugins": ("[]",),
    "mcp_servers": ("[]",),
    "previous_response_id": ("null",),
    "conversation": ("null",),
    "prompt": ("null",),
    "max_tool_calls": ("null",),
    "compaction": ("null",),
}


def _disables(spelling: str, v: Any) -> bool:
    return (spelling == "null" and v is None) or (spelling == "[]" and type(v) is list and not v)


def _field_unbounded(obj: Mapping[str, Any], field: str) -> bool:
    return field in obj and not any(_disables(d, obj[field]) for d in _DISABLED_BY[field])


def _wire_what(name: str, fallback: str) -> str:
    """A refusal's `:<what>` suffix: the client's own name when it fits the wire grammar (`norm.what_ok`), else
    `fallback`. A client may send any string as a field name; `status_of` refuses a suffix it cannot match."""
    return name if norm.what_ok(name) else fallback


def _unbounded_what(name: str) -> str:
    """The `:<what>` suffix of a `body-unbounded` refusal. A found name can be a tool type with a namespace colon
    (`openrouter:web_search`) or a hyphen, which the wire grammar (`norm.what_ok`) refuses, leaving the gateway's
    record with no status; any character outside it becomes `_`."""
    what = re.sub(r"[^a-z0-9_]", "_", name.lower())[:48]
    return what if norm.what_ok(what) else "shape"


def _refuse_json(detail: str) -> norm.Refused:
    return norm.Refused("body-json", detail)


def _reject_constant(name: str) -> Any:
    # json.loads takes NaN / Infinity / -Infinity, and json.dumps would forward them as non-JSON tokens.
    raise ValueError(f"non-JSON constant {name}")


def _finite_float(text: str) -> float:
    # an overflowing literal (`1e999`) parses to inf without calling parse_constant; serialize could not forward it.
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def guarded(fn: Callable[..., T], *args: Any, **kw: Any) -> T:
    """Run one of the steps after `parse` with `RecursionError` mapped to `body-json` (R4 §10)."""
    try:
        return fn(*args, **kw)
    except RecursionError:
        raise _refuse_json("nesting too deep") from None


def refuse_upgrade() -> None:
    """A websocket upgrade on a metered route (R4 §1.6): one exchange carries many responses, no one usage settles it."""
    raise norm.Refused("body-unbounded:websocket", "websocket upgrade on a metered route")


#: re-exported for keys: a route is an OpenRouter route by its pinned upstream host (R4 §1.5).
OPENROUTER_HOST = policy.OPENROUTER_HOST


def write_body_policy(obj: Dict[str, Any], body_policy: Mapping[str, object]) -> None:
    """Head ruling H2, on a dispatch route to OpenRouter: the request's `provider` object is the one the route's
    `body_policy` maps to (`policy.provider_object`), REPLACING whatever `provider` the client sent: a client's object
    is never merged, since a merge would let it widen routing. An empty `body_policy` leaves no `provider` key."""
    obj.pop("provider", None)
    if body_policy:
        obj["provider"] = policy.provider_object(body_policy)


def serialize(obj: Mapping[str, Any]) -> bytes:
    """The forwarded body: keys' own compact serialization of the parsed object (R4 §1.3), so the provider reads the
    object keys estimated. UTF-8, unescaped, so its length is the bytes the provider reads; a lone surrogate the
    parse admitted (`"\\ud800"`) cannot encode and is `body-json`, as is a non-finite number (defensive: `parse`
    refuses both inf spellings)."""
    try:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as e:
        raise _refuse_json(type(e).__name__) from None


def _ceiling_value(field: str, v: Any) -> int:
    # An integral float (1024.0) is the integer it names; anything else that is not a positive integer is refused,
    # because the estimate needs the number the provider will run to.
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise _refuse_json(f"{field} is not a positive integer")
    return v


@dataclass(frozen=True)
class Position:
    """One row of a codec's content-position table: a place the provider's reference documents content blocks.
    `path` is the `_strip_path` grammar and ends at the block (or at the list of them); `admit` is every block `type`
    a dispatch route admits there; `images` are the admit types the estimate counts as an image there (a subset);
    `opaque` are the admit types that carry content the estimate sees only as bytes (a subset)."""
    path: Tuple[Any, ...]
    admit: Tuple[str, ...]
    images: Tuple[str, ...] = ()
    opaque: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Checked:
    """`check`'s answer: the model, its price, and the unboundable content an interactive route admits as
    `"bound": "unproven"` (R4 §1.6). Empty `unproven` = a bounded call."""
    model: str
    price: Price
    unproven: Tuple[str, ...]


class Codec:
    """One `body_codec`. Subclasses name the codec and its shapes; the walk and the arithmetic are shared."""
    name = ""
    #: the POSITION table: every place the provider's reference documents content blocks, with the block types a
    #: dispatch route admits there and which of them are images. ONE table serves both readers: the dispatch walk
    #: (`_refuse_blocks`) and the estimate's image count (`image_paths`, derived from it).
    content_positions: Tuple[Position, ...] = ()
    #: the content-block `type`s the estimate cannot see (documents, provider-fetched files): unproven / refused,
    #: wherever they appear in the body.
    unbounded_blocks: Tuple[str, ...] = ()
    #: block types judged unbounded (server-side history, server-expanded references) that only a position in
    #: `content_positions` reads: they name the refusal `body-unbounded` instead of `body-unlisted`.
    known_unbounded_blocks: Tuple[str, ...] = ()
    #: top-level fields that make the provider add content the estimate cannot see, unless `_DISABLED_BY` disables them.
    unbounded_fields: Tuple[str, ...] = ()
    #: tool types (version suffix removed) that run on the provider and feed results back.
    server_tools: Tuple[str, ...] = ()
    #: tool-type namespaces whose every member runs on the provider: OpenRouter's server tools (`openrouter:*`),
    #: which any codec's body can declare on an OpenRouter route.
    server_tool_prefixes: Tuple[str, ...] = ("openrouter:",)
    #: DISPATCH ALLOWLIST (row 150): the top-level fields a dispatch route admits. A field outside it is
    #: `body-unlisted:<field>`, never judged by default: a field a vendor ships after this list was written is a
    #: refusal, not unmetered spend. It holds every field this codec's own code reads, every `unbounded_fields` entry
    #: (the value check decides those) and each field a reader judged bounded. An interactive route does not read it.
    dispatch_fields: Tuple[str, ...] = ()
    #: fields a vendor publishes that nobody has judged yet: NOT admitted (a dispatch route refuses them
    #: `body-unlisted`), recorded so `scripts/vendor_field_diff.py` reports only what is new since the last triage.
    #: A field moves out of here into `dispatch_fields` (judged bounded) or `unbounded_fields` (judged unbounded).
    unjudged_fields: Tuple[str, ...] = ()
    #: DISPATCH ALLOWLIST, tool types: the client-run `tools[].type`s (version suffix removed) a dispatch route
    #: admits, beside an entry with no `type`. A server tool (`server_tools`, `server_tool_prefixes`) is never here:
    #: it is `body-unbounded`, a known cost, not an unjudged type.
    dispatch_tool_types: Tuple[str, ...] = ()
    #: the output-ceiling fields, in the order `write_ceiling` writes the first.
    ceiling_fields: Tuple[str, ...] = ()
    #: whether a request may lack a ceiling (written in at `default_output`); Anthropic requires `max_tokens`.
    writes_ceiling = False
    #: head ruling (1026+1, settle-check SCOPE): whether keys' settle checks the model a response says ran
    #: (:func:`answered_other`). Only a codec whose route can fall back to another model (OpenRouter's openai-chat),
    #: until 3c-10 measures what real providers put in `model`: an undated alias answered on the primary path would
    #: otherwise stop every session after its first call.
    checks_answered_model = False

    # -- parse ---------------------------------------------------------------------------------------------------

    def parse(self, body: bytes) -> Dict[str, Any]:
        """ONE JSON parse of the buffered body. Anything that does not parse to an object is `body-json` (400),
        including a nesting deep enough to raise `RecursionError` and the non-JSON constants NaN / Infinity."""
        try:
            obj = json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
        except RecursionError:
            raise _refuse_json("nesting too deep") from None
        except (ValueError, UnicodeDecodeError, TypeError) as e:
            raise _refuse_json(type(e).__name__) from None
        if not isinstance(obj, dict):
            raise _refuse_json("not an object")
        return obj

    # -- check ---------------------------------------------------------------------------------------------------

    def check(self, obj: Mapping[str, Any], prices: Optional[Mapping[str, Price]], *, dispatch: bool,
              model_requested: Optional[str] = None) -> Checked:
        """The model has a price entry (else `model-not-allowed`); a ticketed run's `model` is its ticket's
        `model_requested` (else `model-not-allowed`); no `route`, and no `models` but exactly `[model]` (else
        `model-not-allowed`); on a dispatch route, a top-level field, a tool type or a content block the codec has
        not judged is `body-unlisted:<name>` (403); then the unboundable shapes: a dispatch route refuses them
        `body-unbounded:<what>` (403), an interactive one returns them as `unproven`."""
        model = obj.get("model")
        if not isinstance(model, str) or not prices or model not in prices:
            raise norm.Refused("model-not-allowed", "no price entry for the model")
        if model_requested is not None and model != model_requested:
            raise norm.Refused("model-not-allowed", "not the ticket's model")
        # 3c-9 (R4 §5 CARRIED): a provider's fallback list or router choice may run another model than `model`, and
        # OpenRouter prices the request at the model that ran; the hold and the settle price `model` only
        if "route" in obj or ("models" in obj and obj["models"] != [model]):
            raise norm.Refused("model-not-allowed", "a fallback list or route may run another model")
        # OpenRouter's Anthropic-style endpoint: `fallbacks`, "Fallback models to try if the primary model fails or
        # refuses, in order" (openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message.md, read
        # 2026-11-08). Admitted only when it names no other model.
        fb = obj.get("fallbacks")
        if fb is not None and not (isinstance(fb, list) and all(m == model for m in fb)):
            raise norm.Refused("model-not-allowed", "a fallback list may run another model")
        price = prices[model]
        if dispatch:
            self._refuse_unlisted(obj)
        found = self._unbounded(obj, price)
        if found and dispatch:
            raise norm.Refused("body-unbounded:" + _unbounded_what(found[0]),
                               "content the estimate cannot see (a server-side tool or option: turn it off in the "
                               "client, e.g. Codex's `web_search`; a dispatch route admits none)")
        if dispatch:
            self._refuse_blocks(obj)
        return Checked(model, price, found)

    def _refuse_unlisted(self, obj: Mapping[str, Any]) -> None:
        """The dispatch allowlist: the first top-level field outside `dispatch_fields`, then every tool definition the
        body declares (`_tool_lists`: `tools[]` and the codec's inline positions) through `_refuse_tool`. A tool list
        that is not a list judges nothing, so it is refused; a `null` one is no tools, as the provider reads it. The suffix is the name when it fits the wire grammar,
        else a fixed word."""
        for f in obj:
            if f not in self.dispatch_fields:
                raise norm.Refused("body-unlisted:" + _wire_what(f, "field"), "a field levain has not judged; "
                                   "dispatch admits only judged fields")
        for tools in self._tool_lists(obj):
            if tools is None:
                continue
            if not isinstance(tools, list):
                raise norm.Refused("body-unlisted:tools", "a tool list that is not a list; dispatch judges each tool")
            for t in tools:
                self._refuse_tool(t)

    def _refuse_tool(self, t: Any) -> None:
        """One tool definition against `dispatch_tool_types`, wherever the body declared it. A server tool is left to
        `_unbounded`, which names it `body-unbounded`."""
        if not isinstance(t, dict) or "type" not in t:
            return    # a client tool carries no `type`; a non-object is the provider's to refuse
        name = _tool_name(t)
        if name is None:
            raise norm.Refused("body-unlisted:tool", "a tool type levain has not judged")
        if name in self.dispatch_tool_types or name in self.server_tools or name.startswith(self.server_tool_prefixes):
            return
        raise norm.Refused("body-unlisted:" + _wire_what(name, "tool"), "a tool type levain has not judged; "
                           "dispatch admits only judged tool types")

    @property
    def image_paths(self) -> Tuple[Tuple[Tuple[Any, ...], Tuple[str, ...]], ...]:
        """The rows of `content_positions` that hold an image, as (path, the image `type`s there): the estimate's
        image count reads this, so it can only name a position the allowlist names."""
        return tuple((p.path, p.images) for p in self.content_positions if p.images)

    def _refuse_blocks(self, obj: Mapping[str, Any]) -> None:
        """The dispatch allowlist over content: the first block at a documented position that the table does not
        admit there. A known cost is `body-unbounded:<type>`; any other is `body-unlisted:<type>`, with a fixed word
        for a name outside the wire grammar or a block that is no object with a string `type`."""
        for kind, what in self._block_findings(obj):
            if kind == "unbounded":
                raise norm.Refused("body-unbounded:" + _unbounded_what(what),
                                   "content the estimate cannot see (a server-side tool or option: turn it off in the "
                                   "client, e.g. Codex's `web_search`; a dispatch route admits none)")
            raise norm.Refused("body-unlisted:" + _wire_what(what, "block"), "a content block levain has not judged; "
                               "dispatch admits only judged blocks")

    def _block_findings(self, obj: Mapping[str, Any]) -> Iterator[Tuple[str, str]]:
        """Every block at a position of `content_positions` the table does not admit there, in body order, as
        (`unbounded` or `unlisted`, the type). Only the table's positions are read: a block's own fields (a tool call's
        input) are the client's JSON and are never entered."""
        for p in self.content_positions:
            ends_in_list = bool(p.path) and p.path[-1] == EACH
            for b in _walk_path(obj, p.path):
                if not isinstance(b, dict):
                    if ends_in_list:
                        yield "unlisted", ""
                    continue
                t = self._block_type(b, p)
                if t is None:
                    yield "unlisted", ""
                    continue
                fault = self._block_fault(t, b)
                if fault is None and t in p.admit:
                    continue
                if fault == "unbounded" or t in self.unbounded_blocks or t in self.known_unbounded_blocks:
                    yield "unbounded", t
                else:
                    yield "unlisted", t
        yield from self._extra_findings(obj)

    def _block_type(self, b: Mapping[str, Any], p: Position) -> Optional[str]:
        """The `type` of a block at `p`, None when it has no string one."""
        t = b.get("type")
        return t if isinstance(t, str) else None

    def _block_fault(self, t: str, b: Mapping[str, Any]) -> Optional[str]:
        """A block of an admitted type that is not admitted in this form: `unbounded` or `unlisted`; None otherwise."""
        return None

    def _extra_findings(self, obj: Mapping[str, Any]) -> Iterator[Tuple[str, str]]:
        """Findings outside the table's block positions (a reference carried by a field of a message)."""
        return iter(())

    def _tool_lists(self, obj: Mapping[str, Any]) -> List[Any]:
        """Every tool list the body declares, each as the client sent it: top-level `tools` when present, then the
        codec's inline positions (`_inline_tool_lists`). The tool allowlist and the server-tool naming both read it."""
        out: List[Any] = [obj["tools"]] if "tools" in obj else []
        out.extend(self._inline_tool_lists(obj))
        return out

    def _inline_tool_lists(self, obj: Mapping[str, Any]) -> List[Any]:
        """Tool lists a codec's provider documents inside the content (a tool declared mid-conversation)."""
        return []

    def _unbounded(self, obj: Mapping[str, Any], price: Price) -> Tuple[str, ...]:
        found: List[str] = []
        for f in self.unbounded_fields:
            if _field_unbounded(obj, f):
                found.append(f)
        found.extend(f for f in self._unbounded_shapes(obj) if f not in found)
        for tool in self._tools(obj):
            if (tool in self.server_tools or tool.startswith(self.server_tool_prefixes)) and tool not in found:
                found.append(tool)
        for t in self._block_types(obj):
            if t in self.unbounded_blocks and t not in found:
                found.append(t)
        # the position walk's known costs (an unscoped denylist cannot name a type a client's own JSON may hold)
        for kind, what in self._block_findings(obj):
            if kind == "unbounded" and what not in found:
                found.append(what)
        return tuple(found)

    def _unbounded_shapes(self, obj: Mapping[str, Any]) -> List[str]:
        """Top-level fields unbounded only for some of their values, which `_DISABLED_BY`'s spellings cannot name."""
        return []

    def _tools(self, obj: Mapping[str, Any]) -> List[str]:
        """The type names of every tool the body declares (`_tool_lists`), version suffix removed."""
        return [n for tools in self._tool_lists(obj) if isinstance(tools, list)
                for n in map(_tool_name, tools) if n is not None]

    def _block_types(self, obj: Any) -> List[str]:
        """Every `type` string of every object anywhere in the body (a block can nest: a tool result holds blocks)."""
        out: List[str] = []
        stack = [obj]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                t = v.get("type")
                if isinstance(t, str):
                    out.append(t)
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
        return out

    # -- the ceiling ---------------------------------------------------------------------------------------------

    def ceiling(self, obj: Mapping[str, Any], price: Price) -> int:
        """The request's own output ceiling (R4 §1.3): the largest ceiling field present, times `n` when the codec
        has one. A codec that requires a ceiling and has none is estimated at `default_output`; the provider refuses it.
        An explicit `null` ceiling is absent (SDKs send `"max_tokens": null` for "unset")."""
        vals = [_ceiling_value(f, obj[f]) for f in self.ceiling_fields if obj.get(f) is not None]
        return (max(vals) if vals else price.default_output) * self._n(obj)

    def _n(self, obj: Mapping[str, Any]) -> int:
        return 1

    def write_ceiling(self, obj: Dict[str, Any], price: Price) -> bool:
        """Write the ceiling in at `default_output` where the request has none (R4 §1.3, openai-* only). Returns
        whether it wrote. Never shrinks a ceiling the request states. A `null` ceiling is absent and is written over
        (the first null field in `ceiling_fields` order takes the ceiling, any other null one is dropped), because a
        null left in place means unlimited."""
        if not self.writes_ceiling or any(obj.get(f) is not None for f in self.ceiling_fields):
            return False
        nulls = [f for f in self.ceiling_fields if f in obj]
        for f in nulls[1:]:
            del obj[f]
        obj[nulls[0] if nulls else self.ceiling_fields[0]] = price.default_output
        return True

    def write_stream_usage(self, obj: Dict[str, Any]) -> bool:
        """Ask a streamed request for its usage where the provider sends a stream's usage only when asked (openai-chat
        only). Returns whether it wrote."""
        return False

    # -- the estimate --------------------------------------------------------------------------------------------

    def _strip_images(self, obj: Any) -> Tuple[Any, int]:
        """`obj` with the data string of each image at one of this codec's `image_paths` emptied, and the number of
        those images. Only a documented image position is an image to the provider; an image-shaped dict anywhere else
        (a tool's schema, a tool call's input) is text it tokenizes, so its bytes count in full. Copy-on-write: when
        nothing is emptied the answer IS `obj`, so `estimate` can reuse its serialization."""
        n = 0
        for path, types in self.image_paths:
            obj, m = _strip_path(obj, path, types, self._empty_image)
            n += m
        return obj, n

    def _empty_image(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """`d`, an image at an image position, with its image data emptied (`d` itself when it has none to empty)."""
        raise NotImplementedError

    def estimate(self, obj: Mapping[str, Any], price: Price) -> int:
        """R4 §1.2 / §1.3, in integer microdollars, each product rounded up:
        in_est × max(in, cache_write) + ceiling × out + Σ max_uses × per_use, where in_est = the forwarded bytes
        with each image's data excluded + n_images × image_tokens + overhead_tokens. Run it on the object AFTER
        `write_ceiling`, the object keys forwards."""
        fwd = len(serialize(obj))
        if fwd > policy.BODY_CAP_MAX:
            # the forwarded bytes, after write_ceiling: no metered body leaves keys above the provider-side bound.
            raise norm.Refused("body-cap", "serialized body above BODY_CAP_MAX")
        stripped, n_images = self._strip_images(obj)
        in_est = (fwd if stripped is obj else len(serialize(stripped))) + n_images * price.image_tokens \
            + price.overhead_tokens
        total = _ceil_mul(in_est, max(price.in_, price.cache_write))
        total += _ceil_mul(self.ceiling(obj, price), price.out)
        total += self._uses(obj, price)
        return total

    # -- usage ---------------------------------------------------------------------------------------------------

    def call_names(self, obj: Mapping[str, Any], price: Price) -> Dict[str, str]:
        """The `per_use` name each billed response item type settles under, read from the request's own `tools[]`
        through `_tool_name`, the normaliser the estimate's `_uses` keys the hold by: a call is named at the settle as
        its tool was named at the hold. Empty for a codec whose reported usage names its tools itself."""
        return {}

    def usage(self, content_type: bytes, names: Optional[Mapping[str, str]] = None) -> "UsageReader":
        """The side reader for one response (R4 §1.7 step 8): SSE when the response's `content-type` is
        `text/event-stream`, else one JSON body. It reads the bytes the relay passes and never changes them. `names`
        is `call_names` of the request (keys passes it; none = each item type's canonical name)."""
        ct = content_type.split(b";", 1)[0].strip().lower()
        return UsageReader(self, ct == b"text/event-stream", names or {})

    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        """One parsed SSE event; the usage it reports, if any, kept in `st`."""
        raise NotImplementedError

    def _meta_event(self, obj: Mapping[str, Any], md: Dict[str, Any]) -> None:
        """One parsed SSE event; the metadata it states, if any, kept in `md` (H6-6). An OpenAI-shaped chunk states
        it at its top level (`model`; OpenRouter's `provider` and final `usage.cost`), as a non-stream body does."""
        _note_metadata(md, obj)

    def _usage_stream(self, st: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        """The usage of a whole stream from what `_usage_event` kept; None when it reported none. `names` as
        `call_names` answered."""
        raise NotImplementedError

    def _usage_body(self, obj: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        """The usage of a non-stream JSON response; None when it reports none. `names` as `call_names` answered."""
        raise NotImplementedError

    def _uses(self, obj: Mapping[str, Any], price: Price) -> int:
        tools = obj.get("tools")
        total = 0
        if isinstance(tools, list):
            for t in tools:
                name = _tool_name(t)
                if name is None:
                    continue
                uses = t.get("max_uses")
                if name in price.per_use and isinstance(uses, int) and not isinstance(uses, bool) and uses > 0:
                    total += uses * price.per_use[name]
        return total


#: PATH steps: a str is that key of an object; EACH is every element of a list; VALUES is every value of an object;
#: `_Is(...)` continues only on an object whose `type` is one of its names.
EACH = "[]"
VALUES = "{}"


@dataclass(frozen=True)
class _Is:
    types: Tuple[str, ...]


def _walk_path(v: Any, path: Tuple[Any, ...]) -> Iterator[Any]:
    """Every value at the end of `path` (the `_strip_path` grammar), in body order. A step that does not match ends
    that branch. The recursion is as deep as the path, never as deep as the body."""
    if not path:
        yield v
        return
    step, rest = path[0], path[1:]
    if isinstance(step, _Is):
        if isinstance(v, dict) and v.get("type") in step.types:
            yield from _walk_path(v, rest)
    elif step == EACH:
        if isinstance(v, list):
            for x in v:
                yield from _walk_path(x, rest)
    elif step == VALUES:
        if isinstance(v, dict):
            for x in v.values():
                yield from _walk_path(x, rest)
    elif isinstance(v, dict) and step in v:
        yield from _walk_path(v[step], rest)


def _strip_path(v: Any, path: Tuple[Any, ...], types: Tuple[str, ...],
                empty: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Tuple[Any, int]:
    """`v` with each image at the end of `path` (an object whose `type` is in `types`) passed through `empty`, and
    their count. A step that does not match (a missing key, a list where an object is expected) ends that branch with
    nothing stripped. Copy-on-write: an unchanged branch is returned as the same object."""
    if not path:
        if isinstance(v, dict) and v.get("type") in types:
            return empty(v), 1
        return v, 0
    step, rest = path[0], path[1:]
    if isinstance(step, _Is):
        if isinstance(v, dict) and v.get("type") in step.types:
            return _strip_path(v, rest, types, empty)
        return v, 0
    if step == EACH:
        if not isinstance(v, list):
            return v, 0
        out: List[Any] = []
        n = 0
        for x in v:
            y, m = _strip_path(x, rest, types, empty)
            out.append(y)
            n += m
        return (v if all(a is b for a, b in zip(out, v)) else out), n
    if step == VALUES:
        if not isinstance(v, dict):
            return v, 0
        new: Dict[str, Any] = {}
        n = 0
        for k, x in v.items():
            new[k], m = _strip_path(x, rest, types, empty)
            n += m
        return (v if all(new[k] is v[k] for k in v) else new), n
    if not isinstance(v, dict) or step not in v:
        return v, 0
    y, n = _strip_path(v[step], rest, types, empty)
    return (v if y is v[step] else dict(v, **{step: y})), n


def _ceil_mul(tokens: int, rate_per_million: int) -> int:
    return -(-(tokens * rate_per_million) // 1_000_000)


# -- usage (R4 §1.2 "cost = usage priced by class", §1.7 step 8) -----------------------------------------------------

#: the one SSE event the side reader holds, and the non-stream body it copies; past either the exchange has no usage
USAGE_EVENT_MAX = 1 << 20
USAGE_COPY_MAX = 1 << 20
#: the side reader's `stopped` values that are a cap hit
_CAP_STOPS = ("body-over-cap", "event-over-cap")


@dataclass(frozen=True)
class Usage:
    """A response's reported usage, by price class: input tokens at the plain rate, output, cache writes, cache reads,
    and the uses of each priced server tool (named as `per_use` names it)."""
    in_: int
    out: int
    cache_write: int
    cache_read: int
    uses: Tuple[Tuple[str, int], ...] = ()

    def meta(self) -> Dict[str, Any]:
        """The usage as the `settled` line's `meta` records it."""
        out: Dict[str, Any] = {"in": self.in_, "out": self.out, "cache_write": self.cache_write,
                               "cache_read": self.cache_read}
        if self.uses:
            out["uses"] = dict(self.uses)
        return {"usage": out}


def price_usage(u: Usage, price: Price) -> int:
    """`u` priced by class at the entry's rates, in integer microdollars, each product rounded up (`_ceil_mul`, the
    estimate's rounding). A tool use with no `per_use` rate costs nothing here: the estimate held nothing for it."""
    total = (_ceil_mul(u.in_, price.in_) + _ceil_mul(u.out, price.out) + _ceil_mul(u.cache_write, price.cache_write)
             + _ceil_mul(u.cache_read, price.cache_read))
    for tool, n in u.uses:
        total += n * price.per_use.get(tool, 0)
    return total


#: what a response's metadata string must be to be recorded (S1 3c-6c, H6-6): printable ASCII, 1..128 characters,
#: the grammar CONTROL's sink checks `model_answered` / `provider_answered` against. Anything else the response says is
#: recorded as not said (`"unknown"`), never cut or escaped into something it did not say (DECISION).
_ANSWERED = re.compile(r"[\x20-\x7e]{1,128}\Z")
#: the largest reported cost recorded, in microdollars (the wire's JSON_INT_MAX, 2**53 - 1)
_COST_MAX = (1 << 53) - 1


@dataclass(frozen=True)
class Metadata:
    """What a response says about itself (S1 3c-6c, head ruling H6-6: ONE reader, here; 3c-9's settle check reuses it):
    its top-level `model`, the provider that served it (`provider`, OpenRouter's), and the cost it reports
    (`usage.cost`, OpenRouter's usage accounting, in USD credits) as integer microdollars. None = the response did
    not say (the lineage row records `"unknown"` for the two strings and `null` for the cost, H6-21).
    `model_unreadable`: the response did state a top-level `model` key whose value is outside the recorded grammar
    (null and every non-string included, L3 r1 Q3), or stated more distinct models than :data:`MODELS_KEPT` (3c-9:
    the settle cannot rule such a response the checked model, so it fails closed on it; the lineage row still records
    `"unknown"`). `models`: every distinct readable model the response stated, in the order first stated (L3 r1 Q4:
    a stream may name one model in one chunk and another in a later one; `model` is the last, the lineage row's)."""
    model: Optional[str] = None
    provider: Optional[str] = None
    cost_reported: Optional[int] = None
    model_unreadable: bool = False
    models: Tuple[str, ...] = ()


#: L3 r1 Q4, DECISION: the most distinct models one response's metadata keeps; one more is `model_unreadable`
MODELS_KEPT = 8


#: 3c-9: a dated variant of a checked model, as a provider names the snapshot that ran (`<model>-20251001` or
#: `<model>-2025-10-01`); the settle prices such an answer at the checked model's row
_DATED = re.compile(r"-(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2})\Z")


def answered_other(md: Metadata, model: str) -> bool:
    """3c-9 (R4 §5 CARRIED): the response says another model ran than `model`, the checked one: ANY model it stated
    (L3 r1 Q4: every one is kept, `md.models`) is neither `model` nor a dated variant of it, or it stated one outside
    the recorded grammar. A response that names no model is not another model (the codec's own usage is then the only
    evidence, as for every call). Only a codec with :attr:`Codec.checks_answered_model` asks (head ruling 1026+1)."""
    if md.model_unreadable:
        return True
    return any(m != model and not (m.startswith(model) and _DATED.fullmatch(m[len(model):]) is not None)
               for m in (md.models or ((md.model,) if md.model is not None else ())))


def _answered(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _ANSWERED.match(v) else None


def _reported_cost(v: Any) -> Optional[int]:
    """A reported cost in USD as integer microdollars, rounded UP (the estimate's rounding; DECISION); None for
    anything that is not a finite non-negative number (a bool included)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if v < 0:
        return None
    # the decimal the JSON wrote (repr of the parsed float is its shortest round-trip spelling), never binary noise
    micro = (Decimal(repr(v)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING)
    return int(micro) if micro <= _COST_MAX else None


def _note_metadata(md: Dict[str, Any], obj: Any) -> None:
    """The metadata one response object (a body, an SSE chunk, a stream's message or response object) states, kept
    in `md` over what earlier objects stated (a later chunk names the same model; the final one carries the cost)."""
    if not isinstance(obj, dict):
        return
    m, p = _answered(obj.get("model")), _answered(obj.get("provider"))
    if m is not None:
        md["model"] = m
        seen = md.setdefault("models", [])
        if m not in seen:
            if len(seen) < MODELS_KEPT:
                seen.append(m)
            else:
                md["model_unreadable"] = True
    elif "model" in obj:
        # stated, but not readably (null and every non-string included, L3 r1 Q3); sticky: a later readable chunk
        # does not clear it
        md["model_unreadable"] = True
    if p is not None:
        md["provider"] = p
    u = obj.get("usage")
    if isinstance(u, dict):
        c = _reported_cost(u.get("cost"))
        if c is not None:
            md["cost"] = c


class _NoUsage(Exception):
    """A usage object that is not the shape its codec documents: the exchange has no usage."""


def _count(d: Any, key: str, required: bool = False) -> int:
    v = d.get(key) if isinstance(d, dict) else None
    if v is None and not required:
        return 0
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise _NoUsage(key)
    return v


def _plain(total: int, cached: int, written: int, key: str) -> int:
    """The input tokens at the plain rate: an OpenAI-shaped `total` less its cached and cache-written parts. Parts
    that add up to more than the total are a breakdown no provider documents: no usage (charged the hold), never
    clamped to a smaller charge."""
    if cached + written > total:
        raise _NoUsage(key)
    return total - cached - written


def _or_none(fn: Callable[[], Usage]) -> Optional[Usage]:
    try:
        return fn()
    except _NoUsage:
        return None


class UsageReader:
    """One response's side reader. `feed` takes each body read the relay passes (b"" never; the relay's end is
    `result`), keeps at most one SSE event (`USAGE_EVENT_MAX`) or a copy of a non-stream body (`USAGE_COPY_MAX`),
    and never raises: past a cap, or on any error, it stops reading and `result` is None. It buffers only on the
    side; the relayed bytes are the relay's own.

    It is also the ONE response-metadata reader (S1 3c-6c, H6-6): `metadata()` is what the response said about itself
    (`Metadata`), read from the same events and the same copied body. What it read before a cap stopped it is kept (a
    stream's `model` from its first event stays true), so `metadata()` may be called whether or not the relay
    completed."""
    __slots__ = ("_codec", "_stream", "_names", "_buf", "_data", "_held", "_st", "stopped", "_md", "_body_read")

    def __init__(self, c: "Codec", stream: bool, names: Mapping[str, str]) -> None:
        self._codec = c
        self._stream = stream
        self._names = names              # the request's `call_names`
        self._buf = bytearray()          # non-stream: the copied body; SSE: the line not yet ended
        self._data: List[bytes] = []     # SSE: the current event's `data` lines
        self._held = 0                   # SSE: the bytes of those lines
        self._st: Dict[str, Any] = {}    # SSE: what the codec kept from the events so far
        self.stopped: Optional[str] = None
        self._md: Dict[str, Any] = {}    # the metadata read so far (never cleared by a stop)
        self._body_read = False          # non-stream: the copied body has been parsed for its metadata

    def feed(self, part: bytes) -> None:
        if self.stopped is not None:
            return
        try:
            if self._stream:
                self._feed_sse(part)
            elif len(self._buf) + len(part) > USAGE_COPY_MAX:
                self._stop("body-over-cap")
            else:
                self._buf += part
        except Exception as e:   # the side reader never fails the relay
            self._stop(type(e).__name__)

    @property
    def reason(self) -> str:
        """The `uncertain` reason of an exchange whose `result` is None: `usage-over-cap` when either cap stopped the
        reader (a large call whose real cost may pass its hold unseen), else `no-usage`."""
        return "usage-over-cap" if self.stopped in _CAP_STOPS else "no-usage"

    def _stop(self, why: str) -> None:
        self.stopped = why
        self._buf = bytearray()
        self._data = []
        self._st = {}

    def _feed_sse(self, part: bytes) -> None:
        buf = self._buf
        buf += part
        pos = 0
        while True:
            i = buf.find(b"\n", pos)
            if i < 0:
                break
            line = bytes(buf[pos:i])
            pos = i + 1
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                self._dispatch()
            elif not line.startswith(b":"):   # a comment line is skipped
                field, _, value = line.partition(b":")
                if field == b"data":
                    value = value[1:] if value.startswith(b" ") else value
                    self._data.append(value)
                    self._held += len(value) + 1
            if self._held > USAGE_EVENT_MAX:
                self._stop("event-over-cap")
                return
        del buf[:pos]
        if self._held + len(buf) > USAGE_EVENT_MAX:
            self._stop("event-over-cap")

    def _dispatch(self) -> None:
        if not self._data:
            return
        data = b"\n".join(self._data)
        self._data = []
        self._held = 0
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError):
            return   # not a JSON event (openai-chat's `[DONE]`): it reports no usage
        if isinstance(obj, dict):
            self._codec._meta_event(obj, self._md)
            self._codec._usage_event(obj, self._st)

    def _read_body_metadata(self, obj: Any) -> None:
        if not self._body_read:
            self._body_read = True
            _note_metadata(self._md, obj)

    def metadata(self) -> Metadata:
        """What the response said about itself so far (H6-6). A non-stream body is read once, whole, when this or
        `result` first runs after the relay; a body past the copy cap, or one cut short, said nothing readable.
        Known edge (r1 review): called on a non-stream body still being fed, the partial body does not parse and is
        marked read, so the whole body's metadata is never read after it; every caller calls it after the relay."""
        if not self._stream and not self._body_read and self.stopped is None and self._buf:
            try:
                self._read_body_metadata(json.loads(bytes(self._buf)))
            except (ValueError, RecursionError):
                self._body_read = True
        md = self._md
        return Metadata(md.get("model"), md.get("provider"), md.get("cost"), bool(md.get("model_unreadable")),
                        tuple(md.get("models", ())))

    def result(self) -> Optional[Usage]:
        """The reported usage once the relay has ended, or None (no usage: cut, past a cap, none reported). An event
        not ended by a blank line is discarded, as SSE discards it."""
        if self.stopped is not None:
            return None
        try:
            if self._stream:
                return _or_none(lambda: self._codec._usage_stream(self._st, self._names))
            obj = json.loads(bytes(self._buf))
            self._read_body_metadata(obj)
            return _or_none(lambda: self._codec._usage_body(obj, self._names)) if isinstance(obj, dict) else None
        except (ValueError, RecursionError):
            self._body_read = True
            return None
        finally:
            self._buf = bytearray()


class AnthropicMessages(Codec):
    name = "anthropic-messages"
    # The documented content positions of the SDK's request types, and the blocks a dispatch route admits at each: the
    # system text blocks, a message's content, a `tool_result` block's own content, and a `search_result`'s. An image
    # is counted at `image_tokens` whatever its source (base64, url or file). A block outside a position's list is
    # refused there. `thinking` and `redacted_thinking` carry opaque encrypted content the estimate counts by bytes;
    # that is UNMEASURED, so they are admitted and marked `opaque`.
    _RESULT_BLOCKS = ("text", "image", "search_result", "tool_reference")
    content_positions = (
        Position(("system", EACH), ("text",)),
        Position(("messages", EACH, "content", EACH),
                 ("text", "image", "tool_use", "tool_result", "search_result", "tool_addition", "tool_removal",
                  "thinking", "redacted_thinking"), ("image",), ("thinking", "redacted_thinking")),
        Position(("messages", EACH, "content", EACH, _Is(("tool_result",)), "content", EACH),
                 _RESULT_BLOCKS, ("image",)),
        Position(("messages", EACH, "content", EACH, _Is(("tool_result",)), "content", EACH,
                  _Is(("search_result",)), "content", EACH), ("text",)),
        Position(("messages", EACH, "content", EACH, _Is(("search_result",)), "content", EACH), ("text",)),
    )
    # document blocks (PDF, text, URL-fetched) and files a container fetches are content the estimate cannot see.
    unbounded_blocks = ("document", "container_upload")
    # history of a server tool or an MCP server, a server-side compaction and a server-side fallback hop: each is
    # produced only by a feature a dispatch route refuses
    known_unbounded_blocks = (
        "compaction", "server_tool_use", "web_search_tool_result", "web_fetch_tool_result", "code_execution_tool_result",
        "bash_code_execution_tool_result", "text_editor_code_execution_tool_result", "tool_search_tool_result",
        "advisor_tool_result", "mcp_tool_use", "mcp_tool_result", "mcp_tool_listing", "fallback")
    # the MCP connector feeds remote tool results back; OpenRouter's Anthropic-style endpoint takes `plugins` (web,
    # web-fetch, file-parser: openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message.md); an
    # on-demand `compaction` call reports its billed tokens only in `usage.iterations` [read 2026-11-08].
    unbounded_fields = ("mcp_servers", "plugins", "compaction")
    server_tools = ("web_search", "web_fetch", "code_execution")
    ceiling_fields = ("max_tokens",)
    writes_ceiling = False
    # [judged by lane r150, 2026-11-09, from the SDK's request TypedDict at tag v2.7.0 (anthropics/anthropic-sdk-python,
    # `src/anthropic/types/beta/message_create_params.py`), OpenRouter's `MessagesRequest` (openrouter.ai/openapi.json),
    # and the body a real Claude Code sent a local listener (`tests/fixtures/client_bodies/claude_code_*`)]
    dispatch_fields = (
        # the codec's own reads: the model, the ceiling, the stream flag, the tool list, the content the estimate walks
        "model", "max_tokens", "stream", "messages", "system", "tools",
        # value-checked fields (`unbounded_fields`, `_unbounded_shapes`): listed so the value check decides them
        "mcp_servers", "plugins", "compaction", "context_management",
        # bounded by the written ceiling or by the priced-at-highest-tier rule: sampling and output shape; thinking
        # tokens are inside `max_tokens`; `service_tier`, `speed`, `inference_geo` change a tier or region, priced high
        "temperature", "top_p", "top_k", "stop_sequences", "tool_choice", "thinking", "output_config", "output_format",
        "service_tier", "speed", "inference_geo",
        # request labels and prompt-cache hints, no billed content of their own
        "metadata", "cache_control", "diagnostics", "user", "user_profile_id", "workspace_id",
        # OpenRouter's Messages endpoint: routing identity and observability, `models` / `route` / `fallbacks`
        # checked by `check` before this list is read
        "models", "route", "fallbacks", "provider", "session_id", "trace",
    )
    # client-run: Anthropic's `computer`, `bash`, `text_editor` and `memory` tools and a `custom` one are run by the
    # caller; a typeless entry (Claude Code sends every tool so) is admitted by `_refuse_unlisted`
    dispatch_tool_types = ("custom", "bash", "text_editor", "computer", "memory")
    # unmeasured (board row 151 names `container`); `betas` is a header in the SDK; the rest are OpenRouter's server-tool
    # loop and safety controls, and a refusal credit token
    unjudged_fields = ("container", "thread", "fallback_credit_token", "betas", "safeguards", "deferred_tools",
                       "stop_server_tools_when")

    def _unbounded_shapes(self, obj: Mapping[str, Any]) -> List[str]:
        """`context_management` holding a compaction edit (`compact_*`): threshold compaction "requires an additional
        sampling step, which contributes to rate limits and billing", and the top-level usage leaves it out
        (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold, read 2026-11-08). Context
        editing alone (clearing tool uses or thinking) adds no sampling step and stays bounded. A malformed object is
        the provider's to refuse."""
        cm = obj.get("context_management")
        edits = cm.get("edits") if isinstance(cm, dict) else None
        if isinstance(edits, list) and any(
                isinstance(e, dict) and isinstance(e.get("type"), str) and e["type"].startswith("compact")
                for e in edits):
            return ["context_management"]
        return []

    def _block_fault(self, t: str, b: Mapping[str, Any]) -> Optional[str]:
        """A `tool_addition` is admitted when it names a tool already declared (a reference) or defines one, which
        `_refuse_unlisted` has already put through the tool-type allowlist; any other `tool` is unjudged."""
        if t == "tool_addition":
            tool = b.get("tool")
            ok = isinstance(tool, dict) and tool.get("type") in (
                "tool_reference", "mcp_tool_reference", "mcp_toolset_reference", "tool_definition")
            return None if ok else "unlisted"
        return None

    def _inline_tool_lists(self, obj: Mapping[str, Any]) -> List[Any]:
        """A tool declared mid-conversation: a `messages[].content[]` `tool_addition` block whose `tool` is a
        `tool_definition` holds a full tool entry in `definition`, and a `compaction` block's `tool_changes[]` holds
        such additions (the SDK's beta content-block params). A `tool_reference` names a tool already declared."""
        out: List[Any] = []
        messages = obj.get("messages")
        for m in messages if isinstance(messages, list) else ():
            content = m.get("content") if isinstance(m, dict) else None
            for b in content if isinstance(content, list) else ():
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_addition":
                    out.extend(self._definition(b))
                elif b.get("type") == "compaction" and isinstance(b.get("tool_changes"), list):
                    out.extend(d for c in b["tool_changes"]
                               if isinstance(c, dict) and c.get("type") == "tool_addition" for d in self._definition(c))
        return out

    @staticmethod
    def _definition(addition: Mapping[str, Any]) -> List[Any]:
        tool = addition.get("tool")
        if isinstance(tool, dict) and tool.get("type") == "tool_definition":
            return [[tool.get("definition")]]
        return []

    def _empty_image(self, d: Dict[str, Any]) -> Dict[str, Any]:
        src = d.get("source")
        if isinstance(src, dict) and "data" in src:
            return dict(d, source=dict(src, data=""))
        return d

    # Usage (https://platform.claude.com/docs/en/build-with-claude/streaming): `message_start.message.usage`, then the
    # LAST `message_delta.usage`, whose counts "are cumulative", over it: only its keys whose value is not null, so a
    # null in the delta never erases a count the start reported. `input_tokens` excludes the cache classes;
    # `server_tool_use.<tool>_requests` counts a server tool's uses.
    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        t = obj.get("type")
        if t == "message_start" and isinstance(obj.get("message"), dict):
            st["start"] = obj["message"].get("usage")
        elif t == "message_delta":
            st["delta"] = obj.get("usage")

    # Metadata: a stream names its model in `message_start.message` (a non-stream body at its top level); the Messages
    # API names no provider and reports no cost, so those stay unknown.
    def _meta_event(self, obj: Mapping[str, Any], md: Dict[str, Any]) -> None:
        if obj.get("type") == "message_start":
            _note_metadata(md, obj.get("message"))

    def _usage_stream(self, st: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        start, delta = st.get("start"), st.get("delta")
        if not isinstance(start, dict) or not isinstance(delta, dict):
            return None   # a stream with no final `message_delta` reported no final count
        return self._usage(dict(start, **{k: v for k, v in delta.items() if v is not None}))

    def _usage_body(self, obj: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        return self._usage(obj["usage"]) if isinstance(obj.get("usage"), dict) else None

    def _usage(self, u: Mapping[str, Any]) -> Usage:
        stu = u.get("server_tool_use")
        uses: List[Tuple[str, int]] = []
        if isinstance(stu, dict):
            for k in sorted(stu):
                if k.endswith("_requests"):
                    n = _count(stu, k)
                    if n:
                        uses.append((k[:-len("_requests")], n))
        elif stu is not None:
            raise _NoUsage("server_tool_use")
        return Usage(in_=_count(u, "input_tokens", True), out=_count(u, "output_tokens", True),
                     cache_write=_count(u, "cache_creation_input_tokens"),
                     cache_read=_count(u, "cache_read_input_tokens"), uses=tuple(uses))


class _OpenAI(Codec):
    writes_ceiling = True

    def _empty_image(self, d: Dict[str, Any]) -> Dict[str, Any]:
        v = d.get("image_url")
        if isinstance(v, str) and v.startswith("data:"):
            return dict(d, image_url="")
        if isinstance(v, dict) and isinstance(v.get("url"), str) and v["url"].startswith("data:"):
            return dict(d, image_url=dict(v, url=""))
        return d


class OpenAIChat(_OpenAI):
    name = "openai-chat"
    checks_answered_model = True
    # `messages[].content[]`. A `tool` message's content is text parts only (openai-python
    # `ChatCompletionToolMessageParam`), so a tool output holds no image position. A part outside this list (audio,
    # file, a provider's video part) is refused on a dispatch route.
    content_positions = (
        Position(("messages", EACH, "content", EACH), ("text", "refusal", "image_url"), ("image_url",)),
    )
    unbounded_blocks = ("file", "input_audio")
    # `web_search_options` (search models), OpenRouter's `plugins`, and `max_tool_calls` (server-side tool calls)
    # add content the estimate cannot see.
    unbounded_fields = ("web_search_options", "plugins", "max_tool_calls")
    server_tools = ()
    ceiling_fields = ("max_completion_tokens", "max_tokens")
    # [judged by lane r150, 2026-11-09, from `CreateChatCompletionRequest` (github.com/openai/openai-openapi,
    # openapi.yaml) and OpenRouter's `ChatRequest` (openrouter.ai/openapi.json)]
    dispatch_fields = (
        "model", "messages", "stream", "stream_options", "tools", "n", "max_completion_tokens", "max_tokens",
        # value-checked (`unbounded_fields`)
        "web_search_options", "plugins", "max_tool_calls",
        # sampling and output shape, inside the written ceiling; reasoning tokens bill as output
        "temperature", "top_p", "top_k", "min_p", "top_a", "repetition_penalty", "frequency_penalty",
        "presence_penalty", "logit_bias", "logprobs", "top_logprobs", "seed", "stop", "tool_choice",
        "parallel_tool_calls", "function_call", "functions", "response_format", "reasoning_effort", "reasoning",
        "verbosity", "service_tier",
        # labels and cache hints
        "store", "metadata", "user", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "cache_control",
        # OpenRouter routing identity and observability, `models` / `route` checked by `check` before this list
        "models", "route", "provider", "session_id", "trace",
    )
    dispatch_tool_types = ("function", "custom")
    # unmeasured (board row 151: `modalities`, `audio`); a predicted output's rejected tokens bill as output beyond what
    # the ceiling plainly bounds; the rest are OpenRouter's debug, image and server-tool-loop controls
    unjudged_fields = ("audio", "modalities", "moderation", "prediction", "prompt_cache_options", "image_config",
                       "debug", "deferred_tools", "stop_server_tools_when")

    def _extra_findings(self, obj: Mapping[str, Any]) -> Iterator[Tuple[str, str]]:
        """An assistant message's `audio` with an `id` names a previous audio response the provider holds: the
        server expands it into billed audio input the estimate cannot see."""
        messages = obj.get("messages")
        for m in messages if isinstance(messages, list) else ():
            audio = m.get("audio") if isinstance(m, dict) else None
            if isinstance(audio, dict) and "id" in audio:
                yield "unbounded", "audio"

    def write_stream_usage(self, obj: Dict[str, Any]) -> bool:
        """A `stream: true` body with no `stream_options.include_usage` key gets `include_usage: true` written in
        (OpenAI sends a stream's usage only with it; without it the call settles `no-usage` at its hold). A null
        `stream_options` is absent; the client's explicit `include_usage`, and every other `stream_options` key, stand;
        a `stream_options` of any other type is left for the provider to refuse."""
        if obj.get("stream") is not True:
            return False
        opts = obj.get("stream_options")
        if opts is None:
            obj["stream_options"] = {"include_usage": True}
            return True
        if not isinstance(opts, dict) or "include_usage" in opts:
            return False
        opts["include_usage"] = True
        return True

    def _n(self, obj: Mapping[str, Any]) -> int:
        if "n" not in obj:
            return 1
        return max(1, _ceiling_value("n", obj["n"]))

    # Usage: a non-stream body's `usage`; a stream's final chunk's `usage` (OpenAI sends it only with
    # `stream_options.include_usage`; OpenRouter always). `prompt_tokens` counts every input token: the cached ones
    # (`prompt_tokens_details.cached_tokens`) and OpenRouter's cache writes (`cache_write_tokens`,
    # https://openrouter.ai/docs/use-cases/usage-accounting) are priced at their own rates, the rest at `in`
    # [judged by lane 3c2b, 2026-10-23, against those docs: neither page says outright that cache writes are inside
    # `prompt_tokens`; if they are not, the plain input is charged low by them].
    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        if isinstance(obj.get("usage"), dict):
            st["usage"] = obj["usage"]

    def _usage_stream(self, st: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        return self._usage(st["usage"]) if "usage" in st else None

    def _usage_body(self, obj: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        return self._usage(obj["usage"]) if isinstance(obj.get("usage"), dict) else None

    def _usage(self, u: Mapping[str, Any]) -> Usage:
        det = u.get("prompt_tokens_details")
        if det is not None and not isinstance(det, dict):
            raise _NoUsage("prompt_tokens_details")
        cached, written = _count(det, "cached_tokens"), _count(det, "cache_write_tokens")
        return Usage(in_=_plain(_count(u, "prompt_tokens", True), cached, written, "prompt_tokens_details"),
                     out=_count(u, "completion_tokens", True), cache_write=written, cache_read=cached)


class OpenAIResponses(_OpenAI):
    name = "openai-responses"
    # the Responses create reference: a message item's `content[]`; a function / custom tool call output's
    # `output[]` ("string or array of ResponseInputText or ResponseInputImage or ResponseInputFile"); a computer call
    # output's `output` (one `computer_screenshot`); a prompt variable's value ("map[string or ResponseInputText or
    # ResponseInputImage or ResponseInputFile]"). `instructions` is "string or null": no image position.
    # The same positions carry the dispatch allowlist: an `input[]` item (a typeless one with a `role` is a message,
    # one without is a reference to a stored item), then each item's content or output. `reasoning` carries opaque
    # encrypted content the estimate counts by bytes; that is UNMEASURED, so it is admitted and marked `opaque`.
    content_positions = (
        Position(("input", EACH),
                 ("message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
                  "local_shell_call", "local_shell_call_output", "shell_call", "shell_call_output",
                  "apply_patch_call", "apply_patch_call_output", "configuration_update", "reasoning",
                  "tool_search_call", "additional_tools", "tool_search_output"), (), ("reasoning",)),
        Position(("input", EACH, "content", EACH),
                 ("input_text", "output_text", "refusal", "reasoning_text", "input_image"), ("input_image",)),
        Position(("input", EACH, _Is(("function_call_output", "custom_tool_call_output")), "output", EACH),
                 ("input_text", "input_image"), ("input_image",)),
        Position(("input", EACH, _Is(("computer_call_output",)), "output"), ("computer_screenshot",),
                 ("computer_screenshot",)),
        Position(("prompt", "variables", VALUES), ("input_text", "input_image"), ("input_image",)),
    )
    unbounded_blocks = ("input_file",)
    # server tool history, a stored item the server expands, and the compaction items; none is produced by a
    # client-run turn
    known_unbounded_blocks = (
        "item_reference", "compaction", "compaction_trigger", "web_search_call", "file_search_call",
        "code_interpreter_call", "image_generation_call", "computer_call", "computer_call_output", "mcp_list_tools",
        "mcp_approval_request", "mcp_approval_response", "mcp_call", "program", "program_output")
    # server-held state the estimate cannot see (prior turns, a stored prompt), OpenRouter's `plugins`, and
    # `max_tool_calls` (server-side tool calls).
    unbounded_fields = ("previous_response_id", "conversation", "prompt", "plugins", "max_tool_calls")
    server_tools = ("web_search", "web_search_preview", "file_search", "code_interpreter", "computer_use_preview",
                    "mcp", "image_generation")
    ceiling_fields = ("max_output_tokens",)
    # [judged by lane r150, 2026-11-09, from `CreateResponse` (github.com/openai/openai-openapi, openapi.yaml),
    # OpenRouter's `ResponsesRequest` (openrouter.ai/openapi.json), and the body a real Codex (`codex exec`) sent a
    # local listener (`tests/fixtures/client_bodies/codex_*`); `client_metadata` is in neither schema]
    dispatch_fields = (
        "model", "input", "instructions", "stream", "stream_options", "tools", "max_output_tokens",
        # value-checked (`unbounded_fields`)
        "previous_response_id", "conversation", "prompt", "plugins", "max_tool_calls",
        # sampling and output shape, inside the written ceiling; reasoning tokens bill as output; `include` selects
        # extra output fields; `truncation` only shortens the input
        "temperature", "top_p", "top_k", "top_logprobs", "frequency_penalty", "presence_penalty", "stop", "text",
        "reasoning", "include", "tool_choice", "parallel_tool_calls", "truncation", "service_tier",
        # labels and cache hints
        "store", "metadata", "user", "safety_identifier", "prompt_cache_key", "prompt_cache_retention",
        "cache_control",
        # `client_metadata` is outside OpenAI's CreateResponse schema: a client-side field seen in the Codex capture,
        # a label object bounded by the body cap
        "client_metadata",
        # OpenRouter routing identity and observability, `models` / `route` checked by `check` before this list
        "models", "route", "provider", "session_id", "trace",
    )
    # client-run: a `function` or `custom` tool, `local_shell` and `apply_patch` (the model emits the call, the
    # caller runs it), and `namespace` (a group of function tools; Codex declares one, seen in a real capture)
    dispatch_tool_types = ("function", "custom", "namespace", "local_shell", "apply_patch")
    # unmeasured (board row 151: `context_management`, `background`, `moderation`, `modalities`); the rest are
    # OpenAI access programs and image options, and OpenRouter's debug, image and server-tool-loop controls
    unjudged_fields = ("background", "context_management", "moderation", "modalities", "access_programs", "image",
                       "prompt_cache_options", "image_config", "debug", "deferred_tools", "stop_server_tools_when")

    def _block_type(self, b: Mapping[str, Any], p: Position) -> Optional[str]:
        """An `input[]` item with no `type` is classified by shape, as the provider's input union does."""
        if "type" not in b and p.path == ("input", EACH):
            return "message" if "role" in b else "item_reference"
        return super()._block_type(b, p)

    def _block_fault(self, t: str, b: Mapping[str, Any]) -> Optional[str]:
        """A `tool_search_call` is client-run unless it says `server`, which is a provider-side tool run."""
        if t == "tool_search_call" and b.get("execution") == "server":
            return "unbounded"
        return None

    def _inline_tool_lists(self, obj: Mapping[str, Any]) -> List[Any]:
        """A tool declared mid-input: the `tools` of an `additional_tools` or a `tool_search_output` item in `input[]`
        (`ResponseInputItemParam`)."""
        items = obj.get("input")
        return [i["tools"] for i in (items if isinstance(items, list) else ())
                if isinstance(i, dict) and i.get("type") in ("additional_tools", "tool_search_output") and "tools" in i]

    # Usage: a non-stream body's `usage`; a stream's `response.completed` `response.usage`, and
    # `response.incomplete`'s (the call that stopped at its ceiling ends with that event, not `completed`)
    # [judged by lane 3c2b, 2026-10-23, against the Responses streaming reference]. `input_tokens` counts the cached
    # ones (`input_tokens_details.cached_tokens`), priced at `cache_read`, and the cache writes
    # (`input_tokens_details.cache_write_tokens`, when present), priced at `cache_write`; the rest at `in`.
    _FINAL = ("response.completed", "response.incomplete")
    # A server tool's calls are not in `usage`: each is an output item. The items billed per call, by the `per_use`
    # names their tool may be priced under (https://developers.openai.com/api/docs/pricing, "Built-in tools": web search
    # "$10.00 / 1k calls", file search tool call "$2.50 / 1k calls"; code interpreter is billed per container session,
    # not per call, so it is not here). They are counted once per item id, from the non-stream body's `output`, or from
    # a stream's `response.output_item.done` items and its final event's `response.output` together.
    # [judged by lane 3c2b-fix1, 2026-10-23, against that page and https://developers.openai.com/api/docs/guides/
    # tools-web-search ("Search actions incur a tool call cost"): every `web_search_call` item counts as one call
    # whatever its `action`, so an `open_page` / `find_in_page` action is charged high, never low.]
    # The search tool and its preview make the same item type, so a call is named by the tool the REQUEST declared
    # (`call_names`, through `_tool_name`, the name the hold reserved it under); the first name is the one an item
    # takes when the request declared none of them.
    _BILLED_CALLS = {"web_search_call": ("web_search", "web_search_preview"), "file_search_call": ("file_search",)}
    #: the distinct billed items one response may report; past it the exchange has no usage
    BILLED_ITEMS_MAX = 4096

    def call_names(self, obj: Mapping[str, Any], price: Price) -> Dict[str, str]:
        """Each billed item type -> the `per_use` name its calls settle under: the one of its tool names the request's
        `tools[]` declares (`_tool_name`, as `_uses` reads them). Two declared (search and its preview): the item
        cannot say which ran, so the one with the higher `per_use` rate, charged high, never low."""
        declared = self._tools(obj)
        out: Dict[str, str] = {}
        for item_type, tools in self._BILLED_CALLS.items():
            named = [t for t in tools if t in declared]
            if named:
                out[item_type] = max(named, key=lambda t: price.per_use.get(t, 0))
        return out

    # Metadata: a stream's events carry the Response object as `response` (its `model`; OpenRouter's `provider` and
    # its final `usage.cost`); a non-stream body IS that object.
    def _meta_event(self, obj: Mapping[str, Any], md: Dict[str, Any]) -> None:
        _note_metadata(md, obj.get("response"))

    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        calls = st.setdefault("calls", {})
        if obj.get("type") == "response.output_item.done":
            self._call(obj.get("item"), calls)
        elif obj.get("type") in self._FINAL and isinstance(obj.get("response"), dict):
            st["usage"] = obj["response"].get("usage")
            self._calls(obj["response"], calls)

    def _usage_stream(self, st: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        return self._usage(st["usage"], st.get("calls", {}), names) if isinstance(st.get("usage"), dict) else None

    def _usage_body(self, obj: Mapping[str, Any], names: Mapping[str, str]) -> Optional[Usage]:
        if not isinstance(obj.get("usage"), dict):
            return None
        calls: Dict[Any, str] = {}
        self._calls(obj, calls)
        return self._usage(obj["usage"], calls, names)

    def _calls(self, response: Mapping[str, Any], calls: Dict[Any, str]) -> None:
        """Every billed item of a Response object's `output` into `calls`. An `output` that is present and not a
        list is not the documented shape: no usage."""
        out = response.get("output")
        if out is None:
            return
        if not isinstance(out, list):
            raise _NoUsage("output")
        for item in out:
            self._call(item, calls)

    def _call(self, item: Any, calls: Dict[Any, str]) -> None:
        """One output item into `calls` (item id -> item type) when it is billed per call. An item with no string
        id is a call of its own (counted high, never dropped)."""
        if not isinstance(item, dict) or item.get("type") not in self._BILLED_CALLS:
            return
        key = item["id"] if isinstance(item.get("id"), str) else len(calls)
        if key not in calls and len(calls) >= self.BILLED_ITEMS_MAX:
            raise _NoUsage("output")
        calls[key] = item["type"]

    def _usage(self, u: Mapping[str, Any], calls: Mapping[Any, str], names: Mapping[str, str]) -> Usage:
        det = u.get("input_tokens_details")
        if det is not None and not isinstance(det, dict):
            raise _NoUsage("input_tokens_details")
        cached, written = _count(det, "cached_tokens"), _count(det, "cache_write_tokens")
        uses: Dict[str, int] = {}
        for item_type in calls.values():
            name = names.get(item_type, self._BILLED_CALLS[item_type][0])
            uses[name] = uses.get(name, 0) + 1
        return Usage(in_=_plain(_count(u, "input_tokens", True), cached, written, "input_tokens_details"),
                     out=_count(u, "output_tokens", True), cache_write=written, cache_read=cached,
                     uses=tuple(sorted(uses.items())))


#: `body_codec` -> its codec. Every name of `policy.BODY_CODECS` but `none` (a test asserts the equality).
CODECS: Dict[str, Codec] = {c.name: c for c in (AnthropicMessages(), OpenAIChat(), OpenAIResponses())}
