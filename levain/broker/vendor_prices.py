"""The vendor price table (3c R4 §1.3): one entry per model the SHIPPED brokered routes name, each with the pricing
URL it was read from (`source`) and, beside it, the highest-rate choice in one line (which tier, long-context price
or service mode set each rate). Review of this file is the one control on its accuracy (R4 §6). A model with no entry
here is refused `model-not-allowed`.

How the model list is derived (run it at every edit; the table holds exactly the models it prints):

    git grep -nE '"(requested|model)": *"[^"]+"' -- levain/slot levain/broker levain/install*.py

At 3c-2a session i (base 66115cde) it prints nothing: the shipped base map is `{"routes": []}`
(`layout.base_routes_steps`) and no vendor brokered route exists yet (the install helper's `routes-base` verb that
merges them is 3c-9's). The model list is therefore the head's ruling (1023+8 "3c-2a-i RETURNED"): Claude Opus 5.5,
Sonnet 5.5, Haiku 5.5 and OpenRouter z-ai/glm-5.2. A model with no entry is refused.

READ 2026-10-23 (EDT, before 11:30) from the pages named in each `source`, plus:
  https://platform.claude.com/docs/en/about-claude/models/overview  (API ids, max output 128K)
  https://platform.claude.com/docs/en/build-with-claude/vision      (image tokens: high-resolution tier max 4,784)
  https://openrouter.ai/api/v1/models and .../models/z-ai/glm-5.2-20260616/endpoints (provider rows; live, they move)
Units: integer microdollars per million tokens ($1 / MTok = 1_000_000). Anthropic rows are the HIGHEST of: the base
rate; the Opus 5.5 fast-mode row; the 1h cache write; Haiku 5.5's over-100,000-token tier; and the `inference_geo: "us"`
1.1x multiplier, which the page says stacks on fast mode and applies to every token class (and to Haiku's higher tier).
Cache multipliers apply on top of fast mode. Batch is cheaper and is never the highest.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

from typing import Dict

from levain.broker.policy import Price, parse_price

#: model id -> {"price": <a price entry, R4 §1.3>, "choice": "<the highest-rate choice, one line>"}.
VENDOR: Dict[str, dict] = {
    # Opus 5.5 base $4 in / $20 out; fast mode $8 / $40 (the page: "Fast mode pricing applies across the full context
    # window"; cache multipliers and data residency stack on it); 1h write 2x, read 0.05x; US geo 1.1x.
    "claude-opus-5-5": {
        "choice": "fast mode x US geo 1.1: in 8*1.1, out 40*1.1, 1h write (2x) 8*2*1.1, read (0.05x) 8*0.05*1.1",
        "price": {
            "in": 8_800_000, "out": 44_000_000, "cache_write": 17_600_000, "cache_read": 440_000,
            "default_output": 128_000,   # models overview: Max output 128K tokens
            "overhead_tokens": 286,      # pricing page, tool use system prompt tokens, auto/none (any/tool blank for 5.5)
            "image_tokens": 4_784,       # vision page: high-resolution tier, max visual tokens
            "per_use": {"web_search": 10_000},  # $10 per 1,000 searches (not subject to the 1.1x, which is for tokens)
            "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        },
    },
    # Sonnet 5.5 base $2 in / $10 out, 1h write $4, read $0.10; no fast mode listed; US geo 1.1x sets every rate.
    "claude-sonnet-5-5": {
        "choice": "US geo 1.1 on base: in 2*1.1, out 10*1.1, 1h write 4*1.1, read 0.10*1.1 (no fast mode row for Sonnet)",
        "price": {
            "in": 2_200_000, "out": 11_000_000, "cache_write": 4_400_000, "cache_read": 110_000,
            "default_output": 128_000,   # models overview: Max output 128K tokens
            "overhead_tokens": 286,      # pricing page, tool use system prompt tokens, auto/none (any/tool blank for 5.5)
            "image_tokens": 4_784,       # vision page: high-resolution tier, max visual tokens
            "per_use": {"web_search": 10_000},  # $10 per 1,000 searches
            "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        },
    },
    # Haiku 5.5 is priced by prompt length: over 100,000 tokens $0.50 in / $2.50 out / $1 1h write / $0.05 read; x1.1 US.
    "claude-haiku-5-5": {
        "choice": "over-100,000-token tier x US geo 1.1: in 0.50*1.1, out 2.50*1.1, 1h write 1*1.1, read 0.05*1.1",
        "price": {
            "in": 550_000, "out": 2_750_000, "cache_write": 1_100_000, "cache_read": 55_000,
            "default_output": 128_000,   # models overview: Max output 128K tokens
            "overhead_tokens": 406,      # pricing page, tool use system prompt tokens, any/tool (auto/none is 286)
            "image_tokens": 4_784,       # vision page: high-resolution tier, max visual tokens
            "per_use": {"web_search": 10_000},  # $10 per 1,000 searches
            "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        },
    },
    # OpenRouter lists one price per upstream provider. Highest per class over the 33 endpoint rows read on 10-23:
    # in alibaba/fast $2.31, out wafer, cloudflare and decart $8, cache read decart $0.48. No row lists a cache write
    # price, so cache_write is 0 (the estimate takes max(in, cache_write), so the input term is unaffected).
    "z-ai/glm-5.2": {
        "choice": "max over every provider endpoint row: in 2.31 (alibaba/fast), out 8.00 (wafer, cloudflare, decart), read 0.48 (decart)",
        "price": {
            "in": 2_310_000, "out": 8_000_000, "cache_write": 0, "cache_read": 480_000,
            "default_output": 943_718,   # top_provider.max_completion_tokens in the models API (context 1,048,576)
            "overhead_tokens": 0,        # no documented tool-prompt overhead on OpenRouter
            "image_tokens": 1,           # architecture modality text->text: no image input; the field must be positive
            "source": "https://openrouter.ai/z-ai/glm-5.2",
        },
    },
}


def prices() -> Dict[str, Price]:
    """The table parsed: any defective entry raises `PolicyError` (a test parses every entry)."""
    out: Dict[str, Price] = {}
    for model, row in VENDOR.items():
        if set(row) != {"price", "choice"} or not isinstance(row["choice"], str) or not row["choice"].strip():
            raise ValueError(f"vendor price {model}: a row is exactly {{price, choice}} with a one-line choice")
        out[model] = parse_price(f"vendor price {model}", row["price"])
    return out
