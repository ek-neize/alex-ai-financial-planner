"""Unit tests for the tagger's structured output model and its conversion to the DB schema."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent import InstrumentClassification, classification_to_db_format


def make_classification(**overrides):
    data = {
        "symbol": "VEA",
        "name": "Vanguard FTSE Developed Markets ETF",
        "instrument_type": "etf",
        "current_price": 51.25,
        "allocation_asset_class": {"equity": 100},
        "allocation_regions": {"europe": 60, "asia": 30, "global": 10},
        "allocation_sectors": {"financials": 50, "industrials": 50},
    }
    data.update(overrides)
    return InstrumentClassification(**data)


def test_conversion_drops_zero_allocations_and_maps_global_alias():
    instrument = classification_to_db_format(make_classification())

    assert instrument.allocation_asset_class == {"equity": 100}
    assert instrument.allocation_regions == {"europe": 60, "asia": 30, "global": 10}
    assert instrument.allocation_sectors == {"financials": 50, "industrials": 50}
    assert instrument.current_price == Decimal("51.25")


@pytest.mark.parametrize(
    "field,value",
    [
        ("allocation_asset_class", {"equity": 50}),
        ("allocation_regions", {"europe": 150}),
        ("allocation_sectors", {"other": 80}),
    ],
)
def test_allocations_that_do_not_sum_to_100_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_classification(**{field: value})


def test_unknown_allocation_keys_are_rejected():
    # extra="forbid" stops the LLM from inventing categories
    with pytest.raises(ValidationError):
        make_classification(allocation_asset_class={"equity": 90, "crypto": 10})


def test_price_must_be_positive():
    with pytest.raises(ValidationError):
        make_classification(current_price=0)
