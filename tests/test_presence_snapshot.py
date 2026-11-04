"""One coherent presence snapshot: the status view reads the record, the idle time and the remote record once, says what a
delivery would do without writing, and a caller that sampled them can hand them in."""
from __future__ import annotations

import json
import os

from levain.webpush import desk, intent, presence

NOW = 1_800_000_000.0


def _tree(q):
    out = {}
    for dirpath, _d, files in os.walk(q):
        for f in files:
            p = os.path.join(dirpath, f)
            out[p] = open(p, "rb").read()
    return out


def test_the_view_reads_the_record_once_and_samples_idle_once(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    intent.apply_preset(q, "quiet", duration="1h", now=NOW, idle=lambda: 5.0)
    reads, idles = [], []
    real = intent.read

    def racing(queue, now=None):
        reads.append(1)   # the first read sees quiet; any later read would see a concurrent POST's loud
        got = real(queue, now)
        if len(reads) > 1:
            got = {**got, "overlay": {**got["overlay"], "preset": "loud", "lets_through": "everything"}}
        return got

    def idle():
        idles.append(1)
        return 5.0

    monkeypatch.setattr(intent, "read", racing)
    v = intent.view(q, now=NOW + 1, idle=idle)
    assert len(reads) == 1 and len(idles) == 1
    assert v["word"] == "quiet" and v["source"] == "word" and v["state"] == "muted" and "(quiet)" in v["line"]


def test_effective_word_names_the_deciding_overlay_only(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    assert presence.effective(q, idle=lambda: 5.0)["word"] is None
    presence.set_word(q, "loud", duration="1h")
    e = presence.effective(q, idle=lambda: 5.0)
    assert e["source"] == "word" and e["word"] == "loud"


def test_effective_works_from_a_given_snapshot(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    called = []
    e = presence.effective(q, reading=900.0, idle=lambda: called.append(1) or 5.0)
    assert not called and e["state"] == "pushing" and e["reason"] == "follow: no desk message"


def test_a_status_read_writes_nothing_even_with_an_ended_overlay_and_a_crossing(tmp_path):
    """The view is a read: no decision row, no event row, no record write, no stamp (the 11-01 rule: sensing never rewrites)."""
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    intent.apply_preset(q, "away", now=NOW, idle=lambda: 5.0)
    before = _tree(q)
    intent.view(q, now=NOW + 900, idle=lambda: 100.0)         # away is over (input past the grace) and he crossed the threshold
    presence.effective(q, NOW + 900, idle=lambda: 700.0)
    intent.describe_line(q, now=NOW + 900, idle=lambda: 700.0)
    assert _tree(q) == before


def test_the_desk_message_counts_as_input_for_the_view_and_the_delivery_alike(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    desk.note_desk_message(q, "test", now=NOW - 30)
    v = intent.view(q, now=NOW, idle=lambda: 3600.0)
    assert v["state"] == "holding" and v["you"]["state"] == "here"
    assert presence.should_push(q, "ping", now=NOW, idle=lambda: 3600.0, suggest=False).why == v["reason"]
    # a message older than the follow time counts for nothing
    assert intent.view(q, now=NOW + 700, idle=lambda: 3600.0)["state"] == "pushing"


def test_the_record_file_is_never_rewritten_by_reading_it(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    intent.apply_settings(q, devices="follow", follow_after=900, now=NOW, idle=lambda: 5.0)
    raw = (q / "webpush" / intent.FILE_NAME).read_bytes()
    for t in (NOW + 1, NOW + 5000):
        presence.should_push(q, "consent", now=t, idle=lambda: 4000.0)    # a delivery, with its sensing rows
    assert (q / "webpush" / intent.FILE_NAME).read_bytes() == raw
    assert json.loads(raw)["base"]["follow_after"] == 900
