"""D1-N fix round, finding 4: the tick's ``--help`` and the ``latest_link`` docstring say what the code does."""
from __future__ import annotations

import argparse
import inspect

from levain.agents import cli as agents_cli
from levain.agents import tick_driver
from levain.dream import agent as da


def test_the_tick_help_names_the_notice_phase():
    verbs = argparse.ArgumentParser(prog="levain agents").add_subparsers()
    agents_cli._add_tick_parsers(verbs)
    text = " ".join(verbs.choices["tick"].format_help().split())
    assert "notices" in text and "exit code 5" in text and "wrap.lock" in text, text


def test_latest_link_is_documented_as_the_last_expiring_link_not_the_live_one():
    """``binding_of`` arms the link that passes the authority's liveness peek; ``latest_link`` returns the one whose
    ``expires_at`` is last, live or not."""
    src = inspect.getsource(tick_driver.binding_of_factory)
    assert "expires LAST" in src and "live link" not in src
    assert "live link" not in inspect.getsource(da.tick_notices)
