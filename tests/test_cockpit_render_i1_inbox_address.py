"""1012+100 item 1: an inbox row draws `sender -> reader` from its own facets, with or without a compose card.

Found live (flow seat 18:07): a read-only serve declares no compose (no send verb), and the page drew the address
line only `if (addressed)` where addressed meant "the panel declares a recipient field". Every inbox row then showed
no sender, and KE0 (f) failed on any read-only serve. The `for` facet on the rows is what addresses them."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._ke_dom import run_node

BODY = r"""
const row = (id, facets) => ({ id, version: "v", title: "hello " + id, body: "hello " + id, group: null, badges: [], facets: facets });
const heads = { inbox: head("inbox", "list", "Inbox"), plain: head("plain", "list", "Plain") };
const payloads = {
  inbox: head("inbox", "list", "Inbox", { rows: [row("a", { source: "bridge", for: "cli", at: "2026-10-12T11:00:00+00:00" }),
                                                  row("b", { source: "daemon", for: ["cli", "flow"] }), row("c", { source: "argus" })] }),
  plain: head("plain", "list", "Plain", { rows: [row("p", { source: "x", at: "2026-10-12T11:00:00+00:00" })] }),
};
const v = V.fromManifest(fixture(heads, payloads));
const lines = v.extra_panels.inbox.lines;
out.compose = v.extra_panels.inbox.compose || null;
out.addr = lines.map((l) => l.addr ? [l.addr.source, l.addr.to.join(",")] : null);
out.plain = v.extra_panels.plain.lines.map((l) => l.addr || null);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_read_only_inbox_still_draws_sender_to_reader(tmp_path: Path) -> None:
    o = run_node(BODY, tmp_path)
    assert o["compose"] is None                                   # nothing declared a compose
    assert o["addr"] == [["bridge", "cli"], ["daemon", "cli,flow"], ["argus", ""]]
    assert o["plain"] == [None]                                   # a list whose rows carry no `for` is not addressed
