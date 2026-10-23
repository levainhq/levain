"""The vendor price table (3c R4 §1.3): one entry per model the SHIPPED brokered routes name, each with the pricing
URL it was read from (`source`) and, beside it, the highest-rate choice in one line (which tier, long-context price
or service mode set each rate). Review of this file is the one control on its accuracy (R4 §6). A model with no entry
here is refused `model-not-allowed`.

How the model list is derived (run it at every edit; the table holds exactly the models it prints):

    git grep -nE '"(requested|model)": *"[^"]+"' -- levain/slot levain/broker levain/install*.py

At 3c-2a session i (base 66115cde) it prints nothing: the shipped base map is `{"routes": []}`
(`layout.base_routes_steps`) and no vendor brokered route exists yet (the install helper's `routes-base` verb that
merges them is 3c-9's). So the table is empty, and every model is refused until a reviewed entry is added.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

from typing import Dict

from levain.broker.policy import Price, parse_price

#: model id -> {"price": <a price entry, R4 §1.3>, "choice": "<the highest-rate choice, one line>"}.
VENDOR: Dict[str, dict] = {}


def prices() -> Dict[str, Price]:
    """The table parsed: any defective entry raises `PolicyError` (a test parses every entry)."""
    out: Dict[str, Price] = {}
    for model, row in VENDOR.items():
        if set(row) != {"price", "choice"} or not isinstance(row["choice"], str) or not row["choice"].strip():
            raise ValueError(f"vendor price {model}: a row is exactly {{price, choice}} with a one-line choice")
        out[model] = parse_price(f"vendor price {model}", row["price"])
    return out
