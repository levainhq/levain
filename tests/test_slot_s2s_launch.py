"""S1 session 2e lane S: ``levain session claude|codex --repo PATH`` (the launcher's words, refusals and exit codes)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import launch as L
from levain.slot.supervisor import Request


def test_parse_defaults_and_options() -> None:
    assert L.parse(["claude", "--repo", "/r"]) == Request(vendor="claude", repo=Path("/r"))
    assert L.parse(["--repo", "/r", "codex", "--slot", "7", "--auth", "passthrough", "--login"]) == Request(
        vendor="codex", repo=Path("/r"), auth="passthrough", kind="login", slot=7)


@pytest.mark.parametrize("argv,name", [
    ([], "bad-argument"),
    (["claude"], "bad-argument"),                                   # --repo is required
    (["gemini", "--repo", "/r"], "bad-argument"),
    (["claude", "--repo", "/r", "--slot", "100"], "bad-argument"),
    (["claude", "--repo", "/r", "--slot", "07"], "bad-argument"),
    (["claude", "--repo", "/r", "--slot", "-1"], "bad-argument"),
    (["claude", "--repo", "/r", "--slot", "٣"], "bad-argument"),    # a non-ASCII digit
    (["claude", "--repo", "/r", "--auth", "none"], "bad-argument"),
    (["claude", "--repo", "/r", "--login"], "login-needs-passthrough"),
    (["claude", "--repo", "/r", "--bogus"], "bad-argument"),
])
def test_refused_by_name(argv: list[str], name: str) -> None:
    with pytest.raises(Refused) as e:
        L.parse(argv)
    assert e.value.name == name


def test_main_exit_codes() -> None:
    got: list[Request] = []
    err = io.StringIO()
    assert L.main(["claude", "--repo", "/r"], run=lambda r: got.append(r) or 0, err=err) == 0
    assert got == [Request(vendor="claude", repo=Path("/r"))]
    assert L.main(["claude", "--repo", "/r"], run=lambda r: 5, err=err) == 1
    assert L.main(["claude"], run=lambda r: 0, err=err) == 1
    assert "levain session: refused bad-argument" in err.getvalue()
    assert L.main(["--help"], run=lambda r: 0, err=err) == 0


def test_cli_registers_session_and_passes_words_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from levain import cli

    seen: list[list[str]] = []
    monkeypatch.setattr(L, "main", lambda words: seen.append(list(words)) or 0)
    assert cli.main(["session", "--repo", "/r", "claude", "--slot", "2"]) == 0
    assert seen == [["--repo", "/r", "claude", "--slot", "2"]]
