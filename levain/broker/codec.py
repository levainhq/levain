"""The body codecs of a metered brokered route (3c R4 §1.3, §1.6): one class per `body_codec` of `policy.BODY_CODECS`
that can be metered (every one but `none`). keys runs them on the buffered body in §1.7 step 4-5's order:
`parse` (one JSON parse), `check` (the model, the ticket, the unboundable shapes), `write_ceiling` (openai-* only),
`estimate` (the admitted hold). The usage reader is 3c-2b's.

Every refusal is a `norm.Refused`. Any `RecursionError` in these steps is `body-json` (R4 §10), raised by `parse`
for the parse and by `guarded` for the walks after it, so no hold is taken for a body keys cannot walk.

Runs on the root interpreter (3.9): no `match`, no 3.10+ API.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypeVar

from levain.broker import norm
from levain.broker.policy import Price

T = TypeVar("T")

#: a tool type's version suffix (`web_search_20250305` -> `web_search`), the name `per_use` keys a priced tool by.
_TOOL_VERSION = re.compile(r"_\d{8}\Z")


def _refuse_json(detail: str) -> norm.Refused:
    return norm.Refused("body-json", detail)


def _reject_constant(name: str) -> Any:
    # json.loads takes NaN / Infinity / -Infinity, and json.dumps would forward them as non-JSON tokens.
    raise ValueError(f"non-JSON constant {name}")


def guarded(fn: Callable[..., T], *args: Any, **kw: Any) -> T:
    """Run one of the steps after `parse` with `RecursionError` mapped to `body-json` (R4 §10)."""
    try:
        return fn(*args, **kw)
    except RecursionError:
        raise _refuse_json("nesting too deep") from None


def refuse_upgrade() -> None:
    """A websocket upgrade on a metered route (R4 §1.6): one exchange carries many responses, no one usage settles it."""
    raise norm.Refused("body-unbounded:websocket", "websocket upgrade on a metered route")


def serialize(obj: Mapping[str, Any]) -> bytes:
    """The forwarded body: keys' own compact serialization of the parsed object (R4 §1.3), so the provider reads the
    object keys estimated. ASCII-escaped, so a lone surrogate the parse admitted still serializes."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _ceiling_value(field: str, v: Any) -> int:
    # An integral float (1024.0) is the integer it names; anything else that is not a positive integer is refused,
    # because the estimate needs the number the provider will run to.
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise _refuse_json(f"{field} is not a positive integer")
    return v


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
    #: the content-block `type`s that are an image, counted at `image_tokens` each with their data excluded.
    image_types: Tuple[str, ...] = ()
    #: the content-block `type`s the estimate cannot see (documents, provider-fetched files): unproven / refused.
    unbounded_blocks: Tuple[str, ...] = ()
    #: top-level fields whose presence makes the provider add content the estimate cannot see.
    unbounded_fields: Tuple[str, ...] = ()
    #: tool types (version suffix removed) that run on the provider and feed results back.
    server_tools: Tuple[str, ...] = ()
    #: the output-ceiling fields, in the order `write_ceiling` writes the first.
    ceiling_fields: Tuple[str, ...] = ()
    #: whether a request may lack a ceiling (written in at `default_output`); Anthropic requires `max_tokens`.
    writes_ceiling = False

    # -- parse ---------------------------------------------------------------------------------------------------

    def parse(self, body: bytes) -> Dict[str, Any]:
        """ONE JSON parse of the buffered body. Anything that does not parse to an object is `body-json` (400),
        including a nesting deep enough to raise `RecursionError` and the non-JSON constants NaN / Infinity."""
        try:
            obj = json.loads(body, parse_constant=_reject_constant)
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
        `model_requested` (else `model-not-allowed`); then the unboundable shapes: a dispatch route refuses them
        `body-unbounded:<what>` (403), an interactive one returns them as `unproven`."""
        model = obj.get("model")
        if not isinstance(model, str) or not prices or model not in prices:
            raise norm.Refused("model-not-allowed", "no price entry for the model")
        if model_requested is not None and model != model_requested:
            raise norm.Refused("model-not-allowed", "not the ticket's model")
        price = prices[model]
        found = self._unbounded(obj, price)
        if found and dispatch:
            raise norm.Refused(f"body-unbounded:{found[0]}", "content the estimate cannot see")
        return Checked(model, price, found)

    def _unbounded(self, obj: Mapping[str, Any], price: Price) -> Tuple[str, ...]:
        found: List[str] = []
        for f in self.unbounded_fields:
            if obj.get(f) not in (None, False, [], {}):
                found.append(f)
        for tool in self._tools(obj):
            if tool in self.server_tools and tool not in found:
                found.append(tool)
        for t in self._block_types(obj):
            if t in self.unbounded_blocks and t not in found:
                found.append(t)
        return tuple(found)

    def _tools(self, obj: Mapping[str, Any]) -> List[str]:
        tools = obj.get("tools")
        out = []
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict) and isinstance(t.get("type"), str):
                    out.append(_TOOL_VERSION.sub("", t["type"]))
        return out

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
        has one. A codec that requires a ceiling and has none is estimated at `default_output`; the provider refuses it."""
        vals = [_ceiling_value(f, obj[f]) for f in self.ceiling_fields if f in obj]
        return (max(vals) if vals else price.default_output) * self._n(obj)

    def _n(self, obj: Mapping[str, Any]) -> int:
        return 1

    def write_ceiling(self, obj: Dict[str, Any], price: Price) -> bool:
        """Write the ceiling in at `default_output` where the request has none (R4 §1.3, openai-* only). Returns
        whether it wrote. Never shrinks a ceiling the request states."""
        if not self.writes_ceiling or any(f in obj for f in self.ceiling_fields):
            return False
        obj[self.ceiling_fields[0]] = price.default_output
        return True

    # -- the estimate --------------------------------------------------------------------------------------------

    def _strip_images(self, obj: Any) -> Tuple[Any, int]:
        """A copy of `obj` with each image's data string emptied, and the number of images."""
        raise NotImplementedError

    def estimate(self, obj: Mapping[str, Any], price: Price) -> int:
        """R4 §1.2 / §1.3, in integer microdollars, each product rounded up:
        in_est × max(in, cache_write) + ceiling × out + Σ max_uses × per_use, where in_est = the forwarded bytes
        with each image's data excluded + n_images × image_tokens + overhead_tokens. Run it on the object AFTER
        `write_ceiling`, the object keys forwards."""
        stripped, n_images = self._strip_images(obj)
        in_est = len(serialize(stripped)) + n_images * price.image_tokens + price.overhead_tokens
        total = _ceil_mul(in_est, max(price.in_, price.cache_write))
        total += _ceil_mul(self.ceiling(obj, price), price.out)
        total += self._uses(obj, price)
        return total

    def _uses(self, obj: Mapping[str, Any], price: Price) -> int:
        tools = obj.get("tools")
        total = 0
        if isinstance(tools, list):
            for t in tools:
                if not isinstance(t, dict) or not isinstance(t.get("type"), str):
                    continue
                name = _TOOL_VERSION.sub("", t["type"])
                uses = t.get("max_uses")
                if name in price.per_use and isinstance(uses, int) and not isinstance(uses, bool) and uses > 0:
                    total += uses * price.per_use[name]
        return total


def _ceil_mul(tokens: int, rate_per_million: int) -> int:
    return -(-(tokens * rate_per_million) // 1_000_000)


def _walk_copy(v: Any, empty: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> Tuple[Any, int]:
    """Copy `v`, replacing each dict `empty` answers for (an image block, emptied) and counting them. It recurses:
    a body nested deeper than the interpreter's limit raises `RecursionError`, which `guarded` maps to `body-json`."""
    if isinstance(v, dict):
        e = empty(v)
        if e is not None:
            return e, 1
        out: Dict[str, Any] = {}
        n = 0
        for k, x in v.items():
            out[k], m = _walk_copy(x, empty)
            n += m
        return out, n
    if isinstance(v, list):
        items = []
        n = 0
        for x in v:
            y, m = _walk_copy(x, empty)
            items.append(y)
            n += m
        return items, n
    return v, 0


class AnthropicMessages(Codec):
    name = "anthropic-messages"
    image_types = ("image",)
    # document blocks (PDF, text, URL-fetched) and files a container fetches are content the estimate cannot see.
    unbounded_blocks = ("document", "container_upload")
    # the MCP connector feeds remote tool results back.
    unbounded_fields = ("mcp_servers",)
    server_tools = ("web_search", "web_fetch", "code_execution")
    ceiling_fields = ("max_tokens",)
    writes_ceiling = False

    def _strip_images(self, obj: Any) -> Tuple[Any, int]:
        def empty(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if d.get("type") != "image":
                return None
            src = d.get("source")
            if isinstance(src, dict) and "data" in src:
                return dict(d, source=dict(src, data=""))
            return d
        return _walk_copy(obj, empty)


class _OpenAI(Codec):
    writes_ceiling = True

    def _image_url_empty(self, d: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        if d.get("type") not in self.image_types:
            return None
        v = d.get(key)
        if isinstance(v, str) and v.startswith("data:"):
            return dict(d, **{key: ""})
        if isinstance(v, dict) and isinstance(v.get("url"), str) and v["url"].startswith("data:"):
            return dict(d, **{key: dict(v, url="")})
        return d


class OpenAIChat(_OpenAI):
    name = "openai-chat"
    image_types = ("image_url",)
    unbounded_blocks = ("file", "input_audio")
    # `web_search_options` (search models) and OpenRouter's `plugins` add content the estimate cannot see.
    unbounded_fields = ("web_search_options", "plugins")
    server_tools = ()
    ceiling_fields = ("max_completion_tokens", "max_tokens")

    def _n(self, obj: Mapping[str, Any]) -> int:
        if "n" not in obj:
            return 1
        return max(1, _ceiling_value("n", obj["n"]))

    def _strip_images(self, obj: Any) -> Tuple[Any, int]:
        return _walk_copy(obj, lambda d: self._image_url_empty(d, "image_url"))


class OpenAIResponses(_OpenAI):
    name = "openai-responses"
    image_types = ("input_image",)
    unbounded_blocks = ("input_file",)
    # server-held state the estimate cannot see, and OpenRouter's `plugins`.
    unbounded_fields = ("previous_response_id", "conversation", "plugins")
    server_tools = ("web_search", "web_search_preview", "file_search", "code_interpreter", "computer_use_preview",
                    "mcp", "image_generation")
    ceiling_fields = ("max_output_tokens",)

    def _strip_images(self, obj: Any) -> Tuple[Any, int]:
        return _walk_copy(obj, lambda d: self._image_url_empty(d, "image_url"))


#: `body_codec` -> its codec. Every name of `policy.BODY_CODECS` but `none` (a test asserts the equality).
CODECS: Dict[str, Codec] = {c.name: c for c in (AnthropicMessages(), OpenAIChat(), OpenAIResponses())}
