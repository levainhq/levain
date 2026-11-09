"""trimmed snapshot of anthropic-sdk-python v2.7.0 types/beta/message_create_params.py: the request TypedDict's field names"""
from typing import Optional
from typing_extensions import TypedDict


class MessageCreateParamsBase(TypedDict, total=False):
    betas: Optional[str]
    cache_control: Optional[str]
    compaction: Optional[str]
    container: Optional[str]
    context_management: Optional[str]
    diagnostics: Optional[str]
    fallback_credit_token: Optional[str]
    fallbacks: Optional[str]
    inference_geo: Optional[str]
    max_tokens: Optional[str]
    mcp_servers: Optional[str]
    messages: Optional[str]
    metadata: Optional[str]
    model: Optional[str]
    output_config: Optional[str]
    service_tier: Optional[str]
    speed: Optional[str]
    stop_sequences: Optional[str]
    system: Optional[str]
    thinking: Optional[str]
    thread: Optional[str]
    tool_choice: Optional[str]
    tools: Optional[str]
    user_profile_id: Optional[str]
    workspace_id: Optional[str]


class MessageCreateParamsStreaming(MessageCreateParamsBase):
    stream: bool
