"""CH-D2 S8b r1 item 1: a launch runs only a VERIFIED srt install (`srt_pin.check`'s result), checked before any
allocation; a file changed since `levain chat setup` refuses the launch even when srt's version still reads 0.0.81."""

from __future__ import annotations

import os

import pytest

from levain.chat_egress import launch as lm
from levain.chat_egress import srt_pin

from .fixtures import env as envmod
from .test_launch import Origin, _node_children, _start


@pytest.fixture
def origin():
    o = Origin()
    yield o
    o.close()


def _fds() -> set[int]:
    return {int(x) for x in os.listdir("/dev/fd")} - {max(int(x) for x in os.listdir("/dev/fd"))}


@pytest.mark.chat_egress_srt
def test_a_launch_refuses_an_srt_file_changed_since_setup(tmp_path, origin, monkeypatch) -> None:
    prefix = envmod.srt_prefix_copy(tmp_path / "chat-node")
    f = srt_pin.srt_dist(prefix) / "http-proxy.js"
    f.write_text(f.read_text() + "\n// tampered after setup\n")
    assert envmod.srt_version(srt_pin.srt_package(prefix)) == "0.0.81"    # a version check alone passes it
    before, kids = _fds(), _node_children()
    with pytest.raises(lm.EgressRefused, match="hash mismatch") as got:
        lm.stop_egress(_start(tmp_path, origin, monkeypatch, srt_prefix=prefix, events=tmp_path / "ev.jsonl"))
    print(f"RUN S8D item 1: tampered http-proxy.js -> {got.value}")
    assert _fds() == before and len(_node_children()) == len(kids)
    assert not (tmp_path / "ev.jsonl").exists() and not (tmp_path / "ev.jsonl.glue-stderr").exists()
