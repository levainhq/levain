"""The reply text the room reads aloud: fences replaced, chunks deterministic and bounded, nothing lost."""

from __future__ import annotations

import pytest

from levain.voice import text as T


def _words(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


def test_empty_and_blank_input_is_no_chunks():
    for blank in ("", "   ", "\n\n\t \r\n"):
        assert T.chunk_text(blank) == []
        assert T.speakable(blank) == []


def test_short_text_is_one_chunk_and_chunking_is_deterministic():
    assert T.chunk_text("Hello there.") == ["Hello there."]
    body = "First sentence here. " * 80 + "\n\nSecond paragraph starts. " * 40
    assert T.chunk_text(body) == T.chunk_text(body) == T.chunk_text(body[:])
    assert T.speakable(body) == T.speakable(body)


def test_first_chunk_is_short_the_rest_bounded_and_nothing_is_lost():
    body = " ".join(f"This is sentence number {n} of the reply." for n in range(400))
    chunks = T.chunk_text(body)
    assert len(chunks[0]) <= T.FIRST_CHUNK
    assert chunks[0].endswith(".")                       # whole sentences, not a fragment
    assert all(len(c) <= T.CHUNK for c in chunks[1:])
    assert all(len(c) <= T.API_CAP for c in chunks)
    assert _words(chunks) == body.split()
    assert len(chunks) > 2


def test_a_paragraph_break_survives_inside_a_chunk():
    chunks = T.chunk_text("One short line.\n\nTwo short line.")
    assert chunks == ["One short line.\n\nTwo short line."]


def test_a_long_paragraph_with_no_sentence_break_is_cut_at_words():
    body = " ".join(f"word{n}" for n in range(3000))     # ~20k characters, no punctuation at all
    chunks = T.chunk_text(body)
    assert len(chunks[0]) <= T.FIRST_CHUNK
    assert all(len(c) <= T.CHUNK for c in chunks)
    assert _words(chunks) == body.split()
    assert not any(c.startswith("ord") for c in chunks)  # no word was cut in half


def test_a_single_word_over_every_limit_is_hard_cut_without_loss():
    word = "x" * 10_000
    chunks = T.chunk_text(word)
    assert "".join(chunks) == word
    assert all(len(c) <= T.CHUNK for c in chunks) and len(chunks[0]) <= T.FIRST_CHUNK


def test_unicode_cjk_without_spaces_is_split_on_its_sentence_marks():
    body = "今日は晴れです。" * 300                         # no whitespace anywhere; marks only
    chunks = T.chunk_text(body)
    assert "".join(chunks).replace(" ", "") == body     # sentences are joined by a space: harmless to a voice
    assert all(len(c) <= T.CHUNK for c in chunks)
    assert all(c.endswith("。") for c in chunks)


def test_a_combining_mark_is_never_cut_from_its_base():
    word = "é" * 3000
    chunks = T.chunk_text(word)
    assert "".join(chunks) == word
    assert not any(c.startswith("́") for c in chunks)


def test_a_zwj_emoji_sequence_is_never_split():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    word = family * 600
    chunks = T.chunk_text(word)
    assert "".join(chunks) == word
    assert not any(c.startswith("‍") or c.endswith("‍") for c in chunks)


def test_crlf_is_the_same_text_as_lf():
    lf = "A sentence here.\n\nAnother one follows."
    assert T.chunk_text(lf.replace("\n", "\r\n")) == T.chunk_text(lf)


@pytest.mark.parametrize("first,size", [(0, 10), (50, 40), (10, T.API_CAP)])
def test_bad_limits_are_refused(first, size):
    with pytest.raises(ValueError):
        T.chunk_text("anything", first=first, size=size)


@pytest.mark.parametrize("text,expected", [
    ("before\n```python\nprint(1)\n```\nafter", f"before\n{T.CODE_BLOCK_WORDS}\nafter"),
    ("before\n~~~\nx\n~~~\nafter", f"before\n{T.CODE_BLOCK_WORDS}\nafter"),
    ("a\n```\nnever closed\nstill code", f"a\n{T.CODE_BLOCK_WORDS}"),
    ("a\n````\n```\ninner three does not close\n````\nb", f"a\n{T.CODE_BLOCK_WORDS}\nb"),
    ("a\n```\nx\n~~~\nstill code (wrong char)\n```\nb", f"a\n{T.CODE_BLOCK_WORDS}\nb"),
    ("a\n   ```js\nx\n   ```\nb", f"a\n{T.CODE_BLOCK_WORDS}\nb"),
    ("one\n```\nx\n```\nmid\n```\ny\n```\ntwo", f"one\n{T.CODE_BLOCK_WORDS}\nmid\n{T.CODE_BLOCK_WORDS}\ntwo"),
    ("use `x` inline and ```y``` too\nnot a block", "use `x` inline and ```y``` too\nnot a block"),
    ("    ```\nindented four is code in markdown, not a fence\n    ```", "    ```\nindented four is code in markdown, not a fence\n    ```"),
    ("no fences at all", "no fences at all"),
])
def test_code_fences_are_replaced_by_the_fixed_words(text, expected):
    assert T.replace_code_fences(text) == expected


def test_a_fenced_block_is_not_in_the_spoken_chunks():
    chunks = T.speakable("Here is the fix.\n\n```python\nsecret_token = 'abc'\n```\n\nThat is all.")
    spoken = " ".join(chunks)
    assert "secret_token" not in spoken and T.CODE_BLOCK_WORDS in spoken
    assert "Here is the fix." in spoken and "That is all." in spoken


def test_a_reply_past_the_chunk_limit_is_cut_and_says_so():
    body = "\n\n".join(("Paragraph %d. " % n) + ("filler " * 200) for n in range(120))
    chunks = T.speakable(body)
    assert len(chunks) == T.MAX_CHUNKS
    assert chunks[-1] == T.CUT_WORDS
    assert chunks[:-1] == T.chunk_text(T.replace_code_fences(body))[:T.MAX_CHUNKS - 1]


def _stream():
    return [
        {"gseq": 1, "kind": "message", "source": "operator", "body": "first question"},
        {"gseq": 2, "kind": "vendor_agent_text", "source": "vendor", "body": "old answer"},
        {"gseq": 3, "kind": "vendor_turn_ended", "source": "vendor", "body": None},
        {"gseq": 4, "kind": "message", "source": "operator", "body": "second question"},
        {"gseq": 5, "kind": "vendor_agent_text", "source": "vendor", "body": "part one"},
        {"gseq": 6, "kind": "vendor_tool_call", "source": "vendor", "body": "ls"},
        {"gseq": 7, "kind": "vendor_agent_text", "source": "vendor", "body": "part two"},
        {"gseq": 8, "kind": "vendor_turn_ended", "source": "vendor", "body": None},
        {"gseq": 9, "kind": "vendor_agent_text", "source": "vendor", "body": "still running"},
    ]


def test_reply_text_is_the_agent_text_since_the_operators_last_line():
    assert T.reply_text(_stream(), 8) == "part one\n\npart two"
    assert T.reply_text(_stream(), 3) == "old answer"
    assert T.reply_text(reversed(_stream()), 8) == "part one\n\npart two"      # gseq order, not arrival order


def test_reply_text_names_only_a_finished_reply():
    assert T.reply_text(_stream(), 9) is None            # a reply still running is never read
    assert T.reply_text(_stream(), 4) is None            # the operator's own line is not a reply
    assert T.reply_text(_stream(), 99) is None
    assert T.reply_text([], 1) is None


def test_the_chunk_constants_keep_their_ordering():
    assert 0 < T.FIRST_CHUNK <= T.CHUNK < T.API_CAP
