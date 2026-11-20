"""Unit tests for the shared Pydantic schemas used by the API, agents and database."""

import pytest
from pydantic import ValidationError

from src.schemas import InstrumentCreate

VALID = {
    "symbol": "SPY",
    "name": "SPDR S&P 500 ETF",
    "instrument_type": "etf",
    "allocation_regions": {"north_america": 100},
    "allocation_sectors": {"technology": 40, "healthcare": 30, "financials": 30},
    "allocation_asset_class": {"equity": 100},
}


def test_valid_instrument():
    instrument = InstrumentCreate(**VALID)
    assert instrument.symbol == "SPY"
    assert instrument.current_price is None


def test_small_rounding_error_is_allowed():
    InstrumentCreate(**{**VALID, "allocation_sectors": {"technology": 33.3, "healthcare": 33.3, "other": 33.3}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"allocation_asset_class": {"equity": 80}},  # doesn't sum to 100
        {"allocation_regions": {}},  # empty
        {"allocation_regions": {"mars": 100}},  # unknown region
        {"instrument_type": "nft"},  # unknown type
        {"symbol": ""},  # empty symbol
        {"current_price": -1},  # negative price
    ],
)
def test_invalid_instruments_are_rejected(overrides):
    with pytest.raises(ValidationError):
        InstrumentCreate(**{**VALID, **overrides})
