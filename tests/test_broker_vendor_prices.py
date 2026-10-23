"""The vendor price table (3c R4 §1.3). The expected table is a literal, so any price edit is a visible two-place
change (here and in vendor_prices.py)."""
from levain.broker.policy import Price
from levain.broker.vendor_prices import VENDOR, prices

CLAUDE = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
#: model -> (in, out, cache_write, cache_read, default_output, image_tokens, overhead_tokens)
EXPECTED = {
    "claude-opus-5-5": (8_800_000, 44_000_000, 17_600_000, 440_000, 128_000, 4_784, 286),
    "claude-sonnet-5-5": (2_200_000, 11_000_000, 4_400_000, 110_000, 128_000, 4_784, 286),
    "claude-haiku-5-5": (550_000, 2_750_000, 1_100_000, 55_000, 128_000, 4_784, 406),
    "z-ai/glm-5.2": (2_310_000, 8_000_000, 2_887_500, 480_000, 32_768, 4_784, 0),
}


def test_model_set_is_the_heads_list():
    assert set(VENDOR) == set(EXPECTED)
    assert set(prices()) == set(EXPECTED)


def test_every_entry_equals_the_pinned_literal():
    got = {m: (p.in_, p.out, p.cache_write, p.cache_read, p.default_output, p.image_tokens, p.overhead_tokens)
           for m, p in prices().items()}
    assert got == EXPECTED


def test_every_entry_parses_with_positive_rates_and_https_source():
    for model, p in prices().items():
        assert isinstance(p, Price), model
        for v in (p.in_, p.out, p.cache_write, p.cache_read, p.default_output, p.image_tokens):
            assert isinstance(v, int) and not isinstance(v, bool) and v > 0, model
        assert p.source.startswith("https://"), model


def test_claude_rows_per_use_cache_write_and_overhead():
    for model in CLAUDE:
        p = prices()[model]
        assert dict(p.per_use) == {"web_search": 10_000}, model
        assert p.cache_write >= p.in_, model
        assert p.overhead_tokens > 0, model


def test_every_row_names_its_choice():
    for model, row in VENDOR.items():
        assert row["choice"].strip() and "\n" not in row["choice"], model
