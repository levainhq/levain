"""S1 session 2e lane S: the supervisor's terminal filter (BUILD_PLAN §2e 14, §6: OSC 52 and the report sequences are
stripped from the slot's output, titles pass).

Every corpus row is fed whole, at every two-way split point and one byte at a time: the output must be identical,
so no sequence leaks because it straddled two reads and no UTF-8 character is broken by one.
"""
from __future__ import annotations

import pytest

from levain.slot.supervisor import TermFilter

ST = b"\x1b\\"

CORPUS = [
    # OSC 52 (the clipboard write), each terminator
    ("osc52-bel", b"a\x1b]52;c;aGVsbG8=\x07b", b"ab"),
    ("osc52-st", b"a\x1b]52;c;aGVsbG8=" + ST + b"b", b"ab"),
    ("osc52-primary-sel", b"\x1b]52;p;eA==\x07", b""),
    ("osc52-c1-st-not-a-terminator", b"a\x1b]52;c;x\xc2\x9cb\x07c", b"ac"),
    ("osc52-by-c1-introducer", b"a\xc2\x9d52;c;aGk=\x07b", b"a52;c;aGk=\x07b"),
    ("osc52-by-raw-8bit", b"a\x9d52;c;x\x07", b"a52;c;x\x07"),
    ("iterm2-copy", b"\x1b]1337;Copy=:aGk=\x07", b""),
    ("osc-aborted-by-esc", b"\x1b]52;c;x\x1b[31mR", b"\x1b[31mR"),
    ("osc-cancelled", b"\x1b]52;c;x\x18tail", b"tail"),
    ("osc-control-in-payload", b"\x1b]2;a\nb\x07", b""),
    # the report / query requests: his terminal would answer into the slot
    ("da1", b"\x1b[c\x1b[0c", b""),
    ("da2-da3", b"\x1b[>c\x1b[>0c\x1b[=c", b""),
    ("dsr-cpr", b"\x1b[5n\x1b[6n\x1b[?6n\x1b[?996n", b""),
    ("decrqm", b"\x1b[?2004$p\x1b[4$p", b""),
    ("xtversion", b"\x1b[>q\x1b[>0q", b""),
    ("decreqtparm", b"\x1b[x", b""),
    ("kitty-kbd-query", b"\x1b[?u", b""),
    ("title-report", b"\x1b[21t\x1b[20t\x1b[18t\x1b[14t", b""),
    ("decrqcra", b"\x1b[1;1;1;1;1;1*y", b""),
    ("decrqss", b"\x1bP$qm" + ST, b""),
    ("xtgettcap", b"\x1bP+q544e" + ST, b""),
    ("decudk", b"\x1bP0;1|17/ab" + ST, b""),
    ("tmux-passthrough-osc52", b"\x1bPtmux;\x1b\x1b]52;c;aGk=\x07" + ST + b"z", b"z"),
    ("apc-pm-sos", b"\x1b_Gq=1;x" + ST + b"\x1b^pm" + ST + b"\x1bXsos" + ST, b""),
    ("osc-colour-queries", b"\x1b]11;?\x07\x1b]10;?\x07\x1b]4;1;?\x07\x1b]12;?" + ST, b""),
    ("enq-answerback", b"a\x05b", b"ab"),
    ("decid", b"\x1bZ", b""),
    ("c1-csi-cpr", b"\xc2\x9b6n", b"6n"),
    ("raw-c1-csi-cpr", b"\x9b6n", b"6n"),
    ("csi-marker-not-first", b"\x1b[1>6nX", b"X"),
    # what passes untouched
    ("titles", b"\x1b]0;my title\x07\x1b]2;t" + ST + b"\x1b]1;icon\x07", None),
    ("title-utf8", "\x1b]2;café ☕ 𝄞\x07".encode(), None),
    ("hyperlink", b"\x1b]8;;https://example.com\x07link\x1b]8;;\x07", None),
    ("osc-colour-set", b"\x1b]11;#000000\x07\x1b]4;1;rgb:ff/00/00\x07\x1b]104\x07", None),
    ("sgr-and-moves", b"\x1b[1;31mX\x1b[0m\x1b[2J\x1b[H\x1b[10;5H\x1b[?25l\x1b[?2004h\x1b[?1049h", None),
    ("title-stack", b"\x1b[22;0t\x1b[23;0t", None),
    ("charset-and-keypad", b"\x1b(B\x1b)0\x1b=\x1b>\x1b7\x1b8", None),
    ("c0-text", b"line\r\n\ttab\x08\x07", None),
    ("utf8-multibyte", "héllo ☕ 𝄞 ě Ŝ".encode(), None),
]


def _run(chunks: list[bytes]) -> bytes:
    f = TermFilter()
    out = b"".join(f.feed(c) for c in chunks)
    return out + f.flush()


@pytest.mark.parametrize("name,data,want", CORPUS, ids=[c[0] for c in CORPUS])
def test_corpus_whole_split_and_bytewise(name: str, data: bytes, want: bytes | None) -> None:
    want = data if want is None else want
    assert _run([data]) == want
    for i in range(1, len(data)):
        assert _run([data[:i], data[i:]]) == want, f"split at {i}"
    assert _run([data[i:i + 1] for i in range(len(data))]) == want


def test_whole_corpus_in_one_stream() -> None:
    data = b"".join(c[1] for c in CORPUS)
    want = b"".join(c[1] if c[2] is None else c[2] for c in CORPUS)
    assert _run([data]) == want
    for size in (1, 3, 7, 64):
        assert _run([data[i:i + size] for i in range(0, len(data), size)]) == want


def test_multibyte_char_is_never_emitted_in_part() -> None:
    f = TermFilter()
    s = "☕".encode()
    assert f.feed(s[:1]) == b""
    assert f.feed(s[1:2]) == b""
    assert f.feed(s[2:]) == s


def test_cut_multibyte_is_released_at_flush_and_c1_inside_it_dropped() -> None:
    f = TermFilter()
    assert f.feed(b"x\xe2\x82") == b"x"
    assert f.flush() == b"\xe2"            # the lead byte is text (U+FFFD); the 0x82 is a stray C1 byte


def test_unfinished_sequence_is_dropped_at_flush() -> None:
    f = TermFilter()
    assert f.feed(b"ok\x1b]52;c;aGVsbG8=") == b"ok"
    assert f.flush() == b""


def test_long_osc_is_discarded_without_unbounded_buffering() -> None:
    f = TermFilter()
    out = f.feed(b"\x1b]2;" + b"A" * 100_000)
    assert out == b"" and len(f._seq) <= 8192
    assert f.feed(b"\x07after") == b"after"
