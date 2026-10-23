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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypeVar

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
_DISABLED_BY = {
    "web_search_options": (),
    "plugins": ("[]",),
    "mcp_servers": ("[]",),
    "previous_response_id": ("null",),
    "conversation": ("null",),
}


def _disables(spelling: str, v: Any) -> bool:
    return (spelling == "null" and v is None) or (spelling == "[]" and type(v) is list and not v)


def _field_unbounded(obj: Mapping[str, Any], field: str) -> bool:
    return field in obj and not any(_disables(d, obj[field]) for d in _DISABLED_BY[field])


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
class Checked:
    """`check`'s answer: the model, its price, and the unboundable content an interactive route admits as
    `"bound": "unproven"` (R4 §1.6). Empty `unproven` = a bounded call."""
    model: str
    price: Price
    unproven: Tuple[str, ...]


class Codec:
    """One `body_codec`. Subclasses name the codec and its shapes; the walk and the arithmetic are shared."""
    name = ""
    #: the PATH table: every position the provider's reference documents as image-bearing, as (path, the `type`s that
    #: are an image there). An image at one is counted at `image_tokens` with its data excluded (`_strip_images`).
    image_paths: Tuple[Tuple[Tuple[Any, ...], Tuple[str, ...]], ...] = ()
    #: the content-block `type`s the estimate cannot see (documents, provider-fetched files): unproven / refused.
    unbounded_blocks: Tuple[str, ...] = ()
    #: top-level fields that make the provider add content the estimate cannot see, unless `_DISABLED_BY` disables them.
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
            if _field_unbounded(obj, f):
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
        if not isinstance(tools, list):
            return []
        return [n for n in map(_tool_name, tools) if n is not None]

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

    def usage(self, content_type: bytes) -> "UsageReader":
        """The side reader for one response (R4 §1.7 step 8): SSE when the response's `content-type` is
        `text/event-stream`, else one JSON body. It reads the bytes the relay passes and never changes them."""
        ct = content_type.split(b";", 1)[0].strip().lower()
        return UsageReader(self, ct == b"text/event-stream")

    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        """One parsed SSE event; the usage it reports, if any, kept in `st`."""
        raise NotImplementedError

    def _usage_stream(self, st: Mapping[str, Any]) -> Optional[Usage]:
        """The usage of a whole stream from what `_usage_event` kept; None when it reported none."""
        raise NotImplementedError

    def _usage_body(self, obj: Mapping[str, Any]) -> Optional[Usage]:
        """The usage of a non-stream JSON response; None when it reports none."""
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


class _NoUsage(Exception):
    """A usage object that is not the shape its codec documents: the exchange has no usage."""


def _count(d: Any, key: str, required: bool = False) -> int:
    v = d.get(key) if isinstance(d, dict) else None
    if v is None and not required:
        return 0
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise _NoUsage(key)
    return v


def _or_none(fn: Callable[[], Usage]) -> Optional[Usage]:
    try:
        return fn()
    except _NoUsage:
        return None


class UsageReader:
    """One response's side reader. `feed` takes each body read the relay passes (b"" never; the relay's end is
    `result`), keeps at most one SSE event (`USAGE_EVENT_MAX`) or a copy of a non-stream body (`USAGE_COPY_MAX`),
    and never raises: past a cap, or on any error, it stops reading and `result` is None. It buffers only on the
    side; the relayed bytes are the relay's own."""
    __slots__ = ("_codec", "_stream", "_buf", "_data", "_held", "_st", "stopped")

    def __init__(self, c: "Codec", stream: bool) -> None:
        self._codec = c
        self._stream = stream
        self._buf = bytearray()          # non-stream: the copied body; SSE: the line not yet ended
        self._data: List[bytes] = []     # SSE: the current event's `data` lines
        self._held = 0                   # SSE: the bytes of those lines
        self._st: Dict[str, Any] = {}    # SSE: what the codec kept from the events so far
        self.stopped: Optional[str] = None

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
            self._codec._usage_event(obj, self._st)

    def result(self) -> Optional[Usage]:
        """The reported usage once the relay has ended, or None (no usage: cut, past a cap, none reported). An event
        not ended by a blank line is discarded, as SSE discards it."""
        if self.stopped is not None:
            return None
        try:
            if self._stream:
                return _or_none(lambda: self._codec._usage_stream(self._st))
            obj = json.loads(bytes(self._buf))
            return _or_none(lambda: self._codec._usage_body(obj)) if isinstance(obj, dict) else None
        except (ValueError, RecursionError):
            return None
        finally:
            self._buf = bytearray()


class AnthropicMessages(Codec):
    name = "anthropic-messages"
    # `messages[].content[]`, and a `tool_result` block's own `content[]` inside it.
    image_paths = (
        (("messages", EACH, "content", EACH), ("image",)),
        (("messages", EACH, "content", EACH, _Is(("tool_result",)), "content", EACH), ("image",)),
    )
    # document blocks (PDF, text, URL-fetched) and files a container fetches are content the estimate cannot see.
    unbounded_blocks = ("document", "container_upload")
    # the MCP connector feeds remote tool results back.
    unbounded_fields = ("mcp_servers",)
    server_tools = ("web_search", "web_fetch", "code_execution")
    ceiling_fields = ("max_tokens",)
    writes_ceiling = False

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

    def _usage_stream(self, st: Mapping[str, Any]) -> Optional[Usage]:
        start, delta = st.get("start"), st.get("delta")
        if not isinstance(start, dict) or not isinstance(delta, dict):
            return None   # a stream with no final `message_delta` reported no final count
        return self._usage(dict(start, **{k: v for k, v in delta.items() if v is not None}))

    def _usage_body(self, obj: Mapping[str, Any]) -> Optional[Usage]:
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
    # `messages[].content[]`. A `tool` message's content is text parts only (openai-python
    # `ChatCompletionToolMessageParam`), so a tool output holds no image position.
    image_paths = ((("messages", EACH, "content", EACH), ("image_url",)),)
    unbounded_blocks = ("file", "input_audio")
    # `web_search_options` (search models) and OpenRouter's `plugins` add content the estimate cannot see.
    unbounded_fields = ("web_search_options", "plugins")
    server_tools = ()
    ceiling_fields = ("max_completion_tokens", "max_tokens")

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

    def _usage_stream(self, st: Mapping[str, Any]) -> Optional[Usage]:
        return self._usage(st["usage"]) if "usage" in st else None

    def _usage_body(self, obj: Mapping[str, Any]) -> Optional[Usage]:
        return self._usage(obj["usage"]) if isinstance(obj.get("usage"), dict) else None

    def _usage(self, u: Mapping[str, Any]) -> Usage:
        det = u.get("prompt_tokens_details")
        if det is not None and not isinstance(det, dict):
            raise _NoUsage("prompt_tokens_details")
        cached, written = _count(det, "cached_tokens"), _count(det, "cache_write_tokens")
        return Usage(in_=max(0, _count(u, "prompt_tokens", True) - cached - written),
                     out=_count(u, "completion_tokens", True), cache_write=written, cache_read=cached)


class OpenAIResponses(_OpenAI):
    name = "openai-responses"
    # the Responses create reference: a message item's `content[]`; a function / custom tool call output's
    # `output[]` ("string or array of ResponseInputText or ResponseInputImage or ResponseInputFile"); a computer call
    # output's `output` (one `computer_screenshot`); a prompt variable's value ("map[string or ResponseInputText or
    # ResponseInputImage or ResponseInputFile]"). `instructions` is "string or null": no image position.
    image_paths = (
        (("input", EACH, "content", EACH), ("input_image",)),
        (("input", EACH, _Is(("function_call_output", "custom_tool_call_output")), "output", EACH), ("input_image",)),
        (("input", EACH, _Is(("computer_call_output",)), "output"), ("computer_screenshot",)),
        (("prompt", "variables", VALUES), ("input_image",)),
    )
    unbounded_blocks = ("input_file",)
    # server-held state the estimate cannot see, and OpenRouter's `plugins`.
    unbounded_fields = ("previous_response_id", "conversation", "plugins")
    server_tools = ("web_search", "web_search_preview", "file_search", "code_interpreter", "computer_use_preview",
                    "mcp", "image_generation")
    ceiling_fields = ("max_output_tokens",)

    # Usage: a non-stream body's `usage`; a stream's `response.completed` `response.usage`, and
    # `response.incomplete`'s (the call that stopped at its ceiling ends with that event, not `completed`)
    # [judged by lane 3c2b, 2026-10-23, against the Responses streaming reference]. `input_tokens` counts the cached
    # ones (`input_tokens_details.cached_tokens`), priced at `cache_read`.
    _FINAL = ("response.completed", "response.incomplete")

    def _usage_event(self, obj: Mapping[str, Any], st: Dict[str, Any]) -> None:
        if obj.get("type") in self._FINAL and isinstance(obj.get("response"), dict):
            st["usage"] = obj["response"].get("usage")

    def _usage_stream(self, st: Mapping[str, Any]) -> Optional[Usage]:
        return self._usage(st["usage"]) if isinstance(st.get("usage"), dict) else None

    def _usage_body(self, obj: Mapping[str, Any]) -> Optional[Usage]:
        return self._usage(obj["usage"]) if isinstance(obj.get("usage"), dict) else None

    def _usage(self, u: Mapping[str, Any]) -> Usage:
        det = u.get("input_tokens_details")
        if det is not None and not isinstance(det, dict):
            raise _NoUsage("input_tokens_details")
        cached = _count(det, "cached_tokens")
        return Usage(in_=max(0, _count(u, "input_tokens", True) - cached), out=_count(u, "output_tokens", True),
                     cache_write=0, cache_read=cached)


#: `body_codec` -> its codec. Every name of `policy.BODY_CODECS` but `none` (a test asserts the equality).
CODECS: Dict[str, Codec] = {c.name: c for c in (AnthropicMessages(), OpenAIChat(), OpenAIResponses())}
