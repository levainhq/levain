"""Fixtures and gates for the chat-egress suite (CH-D2 S1).

The suite runs the SPIKE glue (`fixtures/spike_glue/`) over a real @anthropic-ai/sandbox-runtime 0.0.81. Three gates:
  chat_egress_srt      needs node + srt 0.0.81 (CHD2_SRT_DIST); skipped, with the reason, where they are absent.
  chat_egress_network  needs the public internet (one read-only `git clone` from github.com, one GET of example.com);
                       skipped when CI is set or CHD2_SKIP_NETWORK=1.
  chat_egress_slow     the 30,000-branch git dumps (minutes); skipped when CI is set unless CHD2_SLOW=1.
`verdict(...)` records "recorded vs reproduced" pairs; the terminal summary prints them (and CHD2_VERDICT_FILE gets them).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .fixtures import env as envmod
from .fixtures.stack import Rig

_LEDGER: list[tuple[str, str, str, str]] = []


def pytest_configure(config):
    config.addinivalue_line("markers", "chat_egress_srt: needs node and @anthropic-ai/sandbox-runtime 0.0.81")
    config.addinivalue_line("markers", "chat_egress_network: needs the public internet; skipped in CI")
    config.addinivalue_line("markers", "chat_egress_slow: minutes-long git dumps; skipped in CI unless CHD2_SLOW=1")


def pytest_collection_modifyitems(config, items):
    # This hook sees the whole session's items, so the srt probe is lazy: it runs only when a collected item carries
    # the chat_egress_srt marker (an unrelated run never shells out to node), and at most once.
    srt_reason: list[str | None] = []
    in_ci = bool(os.environ.get("CI"))
    for item in items:
        if item.get_closest_marker("chat_egress_srt"):
            if not srt_reason:
                srt_reason.append(envmod.srt_skip_reason())
            if srt_reason[0]:
                item.add_marker(pytest.mark.skip(reason=srt_reason[0]))
        if item.get_closest_marker("chat_egress_network") and (in_ci or os.environ.get("CHD2_SKIP_NETWORK") == "1"):
            item.add_marker(pytest.mark.skip(reason="needs the public internet (CI or CHD2_SKIP_NETWORK=1)"))
        if item.get_closest_marker("chat_egress_slow") and in_ci and os.environ.get("CHD2_SLOW") != "1":
            item.add_marker(pytest.mark.skip(reason="minutes-long dump; CI skips it unless CHD2_SLOW=1"))


@pytest.fixture
def verdict(request):
    """verdict(probe_id, recorded, reproduced): one row of the recorded-vs-reproduced ledger."""
    def put(probe: str, recorded: str, reproduced: str) -> None:
        _LEDGER.append((probe, recorded, reproduced, request.node.nodeid))
    return put


def pytest_terminal_summary(terminalreporter):
    if not _LEDGER:
        return
    terminalreporter.section("chat-egress: recorded vs reproduced verdicts")
    lines = []
    for probe, rec, repro, node in _LEDGER:
        lines.append(f"{probe}\n    recorded:   {rec}\n    reproduced: {repro}")
    for ln in lines:
        terminalreporter.write_line(ln)
    out = os.environ.get("CHD2_VERDICT_FILE")
    if out:
        Path(out).write_text("\n".join(lines) + "\n")


def build_rig(tmp_path_factory, name: str, origin: str | None = "bytes", dns: bool = True, raw: bool = False) -> Rig:
    """Module-scoped rig builder for probe modules: PKI + (DNS) + (origin) started, nothing else."""
    rig = Rig(tmp_path_factory.mktemp(f"egress-{name}"))
    try:
        rig.start_pki()
        if dns:
            rig.start_dns()
        if origin:
            rig.start_origin(origin)
        if raw:
            rig.start_raw_listener()
    except BaseException:
        rig.stop()
        raise
    return rig


def pytest_make_parametrize_id(config, val, argname):
    """Probe parameters carry the recorded verdict text; keep the node id short and shell-safe."""
    if isinstance(val, str) and len(val) > 28:
        import re
        return re.sub(r"\W+", "-", val)[:28].strip("-") or argname
    return None
