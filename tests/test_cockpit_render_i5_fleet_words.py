"""1012+100 item 5 (desk 18:20 KE4 deltas): a resident's tags line says "memory" (mock-e), not the store size
("memory 369 kB"), which stays an on-call fact; the Fleet filter's placeholder is "filter agents…" (mock-e), every
other panel keeps "Filter"."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._ke_dom import run_node

BODY = r"""
const row = (name, group, memory) => ({ name: name, role: "main", memory: "stateful", cadence: group === "resident" ? "resident" : "on_call", group: null,
  lifecycle: "active", wrap_every: null, silence_is_signal: false, fleet_group: group,
  health: { tone: "ok", text: "answered", at: null, memory: memory } });
const heads = { fleet: head("fleet", "roster", "Fleet", { searchable: true, search: { fields: [], scope: "rows", default_visible: 40, hook: false } }),
                mine: head("mine", "list", "Mine", { searchable: true, search: { fields: [], scope: "rows", default_visible: 10, hook: false } }) };
const payloads = { fleet: head("fleet", "roster", "Fleet", { searchable: true, search: { fields: [], scope: "rows", default_visible: 40, hook: false },
    value: { rows: [row("flow", "resident", "memory 369 kB"), row("codex", "on_call", "memory 2.0 MB"), row("gemini", "on_call", "stateless"), row("res0", "resident", "stateless")] } }),
  mine: head("mine", "list", "Mine", { searchable: true, search: { fields: [], scope: "rows", default_visible: 10, hook: false }, rows: [] }) };
const v = V.fromManifest(fixture(heads, payloads));
const host = new N("div");
K.fill(host, v.extra_panels.fleet, { entry: { title: "Fleet" } });
out.tags = host.all((n) => n.className === "tags").map((n) => n.textContent);
out.placeholder = host.all((n) => n.tag === "input").map((n) => n.placeholder);
const h2 = new N("div");
K.fill(h2, v.extra_panels.mine, { entry: { title: "Mine" } });
out.trayPlaceholder = h2.all((n) => n.tag === "input").map((n) => n.placeholder);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_resident_tags_say_memory_and_the_fleet_filter_names_agents(tmp_path: Path) -> None:
    o = run_node(BODY, tmp_path)
    assert o["tags"] == ["main · memory", "main · stateless", "main · memory 2.0 MB", "main · stateless"]
    assert o["placeholder"] == ["filter agents…"]
    assert o["trayPlaceholder"] == ["Filter"]
