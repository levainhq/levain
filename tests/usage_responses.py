"""Upstream responses that report usage, one per codec, stream and non-stream, in the shape each provider documents
(sources in each comment). Written from the docs, not captured from a provider (a real provider call settling from
real usage is 3c-10's); lane 3c2b's step-1 run relayed each through keys as a child process and recorded the bytes
the front received (`project_memory/1023-15/3c2b/run1_main_ledger.json`), which are `body(...)` below."""

import json


def _sse(events):
    """SSE bytes: each (event-name or None, data object or raw str) as `event:` / `data:` lines and a blank line."""
    out = b""
    for name, data in events:
        if name is not None:
            out += b"event: " + name.encode() + b"\n"
        text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        out += b"data: " + text.encode() + b"\n\n"
    return out


def _http(body: bytes, ctype: bytes, chunked: bool) -> bytes:
    head = b"HTTP/1.1 200 OK\r\ncontent-type: " + ctype + b"\r\nconnection: close\r\n"
    if not chunked:
        return head + b"content-length: %d\r\n\r\n" % len(body) + body
    # two chunks, split mid-body, so the side reader sees an event cut across reads
    half = len(body) // 2
    out = head + b"transfer-encoding: chunked\r\n\r\n"
    for part in (body[:half], body[half:]):
        out += b"%x\r\n" % len(part) + part + b"\r\n"
    return out + b"0\r\n\r\n"


# https://platform.claude.com/docs/en/api/messages (the Message object's `usage`), and
# https://platform.claude.com/docs/en/build-with-claude/streaming (message_start / message_delta, "cumulative")
ANTHROPIC_JSON = {
    "id": "msg_013Zva2CMHLNnXjNJJKqJ2EF", "type": "message", "role": "assistant", "model": "m1",
    "content": [{"type": "text", "text": "Hi! My name is Claude."}], "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 2095, "cache_creation_input_tokens": 2051, "cache_read_input_tokens": 4000,
              "output_tokens": 503, "server_tool_use": {"web_search_requests": 2}}}
ANTHROPIC_SSE = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1nZdL29xx5MUA1yADyHTEsnR8uuvGzszyY", "type": "message", "role": "assistant", "content": [],
        "model": "m1", "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 472, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200,
                  "output_tokens": 2}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("ping", {"type": "ping"}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "Okay"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 15}}),
    ("message_stop", {"type": "message_stop"}),
]

# https://platform.openai.com/docs/api-reference/chat/object (usage, prompt_tokens_details.cached_tokens) and
# https://platform.openai.com/docs/api-reference/chat-streaming (the final chunk's `usage`, stream_options.include_usage);
# OpenRouter adds `usage.cost` (https://openrouter.ai/docs/use-cases/usage-accounting)
OPENAI_CHAT_JSON = {
    "id": "chatcmpl-B9MHDbslfkBeAs8l4bebGdFOJ6PeG", "object": "chat.completion", "created": 1741570283,
    "model": "m1", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"},
                                "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1117, "completion_tokens": 46, "total_tokens": 1163,
              "prompt_tokens_details": {"cached_tokens": 1024, "audio_tokens": 0},
              "completion_tokens_details": {"reasoning_tokens": 20, "audio_tokens": 0}}}
OPENAI_CHAT_SSE = [
    (None, {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            "usage": None}),
    (None, {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}], "usage": None}),
    (None, {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m1", "choices": [],
            "usage": {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340,
                      "prompt_tokens_details": {"cached_tokens": 128}, "cost": 0.00042}}),
    (None, "[DONE]"),
]

# https://platform.openai.com/docs/api-reference/responses/object (usage, input_tokens_details.cached_tokens) and
# https://platform.openai.com/docs/api-reference/responses-streaming (response.completed)
_RESP_USAGE = {"input_tokens": 328, "input_tokens_details": {"cached_tokens": 64}, "output_tokens": 52,
               "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 380}
OPENAI_RESPONSES_JSON = {
    "id": "resp_67ccd3a9da748190baa7f1570fe91ac6", "object": "response", "created_at": 1741476777,
    "status": "completed", "model": "m1",
    "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi", "annotations": []}]}],
    "usage": _RESP_USAGE}
OPENAI_RESPONSES_SSE = [
    ("response.created", {"type": "response.created", "sequence_number": 0,
                          "response": {"id": "resp_1", "object": "response", "status": "in_progress", "usage": None}}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg_1",
                                    "output_index": 0, "content_index": 0, "delta": "Hi"}),
    ("response.completed", {"type": "response.completed", "sequence_number": 2,
                            "response": {"id": "resp_1", "object": "response", "status": "completed",
                                         "usage": _RESP_USAGE}}),
]

#: (codec, stream) -> (request body, the upstream's whole HTTP response)
CASES = {
    ("anthropic-messages", False): (b'{"model":"m1","max_tokens":10,"messages":[]}',
                                    _http(json.dumps(ANTHROPIC_JSON).encode(), b"application/json", False)),
    ("anthropic-messages", True): (b'{"model":"m1","max_tokens":10,"messages":[],"stream":true}',
                                   _http(_sse(ANTHROPIC_SSE), b"text/event-stream; charset=utf-8", True)),
    ("openai-chat", False): (b'{"model":"m1","max_completion_tokens":10,"messages":[]}',
                             _http(json.dumps(OPENAI_CHAT_JSON).encode(), b"application/json", False)),
    ("openai-chat", True): (b'{"model":"m1","max_completion_tokens":10,"messages":[],"stream":true,'
                            b'"stream_options":{"include_usage":true}}',
                            _http(_sse(OPENAI_CHAT_SSE), b"text/event-stream", True)),
    ("openai-responses", False): (b'{"model":"m1","max_output_tokens":10,"input":"hi"}',
                                  _http(json.dumps(OPENAI_RESPONSES_JSON).encode(), b"application/json", False)),
    ("openai-responses", True): (b'{"model":"m1","max_output_tokens":10,"input":"hi","stream":true}',
                                 _http(_sse(OPENAI_RESPONSES_SSE), b"text/event-stream", True)),
}


def body(codec: str, stream: bool) -> bytes:
    """The response body the relay passes for one case (the HTTP framing removed, a chunked body de-chunked)."""
    if stream:
        return _sse({"anthropic-messages": ANTHROPIC_SSE, "openai-chat": OPENAI_CHAT_SSE,
                     "openai-responses": OPENAI_RESPONSES_SSE}[codec])
    return json.dumps({"anthropic-messages": ANTHROPIC_JSON, "openai-chat": OPENAI_CHAT_JSON,
                       "openai-responses": OPENAI_RESPONSES_JSON}[codec]).encode()
