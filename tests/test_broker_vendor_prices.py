"""The vendor price table (3c R4 §1.3): every entry parses, rates are positive ints, sources are https, and the
model set is the head's list, so adding or dropping a model is a visible edit here."""
from levain.broker.policy import Price
from levain.broker.vendor_prices import VENDOR, prices

MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "z-ai/glm-5.2"}


def test_model_set_is_the_heads_list():
    assert set(VENDOR) == MODELS
    assert set(prices()) == MODELS


def test_every_entry_parses_with_positive_rates():
    for model, p in prices().items():
        assert isinstance(p, Price), model
        for v in (p.in_, p.out, p.cache_read, p.default_output, p.image_tokens):
            assert isinstance(v, int) and not isinstance(v, bool) and v > 0, model
        assert p.cache_write >= 0 and p.overhead_tokens >= 0, model
        assert p.source.startswith("https://"), model


def test_every_row_names_its_choice():
    for model, row in VENDOR.items():
        assert row["choice"].strip() and "\n" not in row["choice"], model
