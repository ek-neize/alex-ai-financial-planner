"""Unit tests for the charter agent's portfolio analysis (no AWS needed)."""

from agent import analyze_portfolio

PORTFOLIO = {
    "accounts": [
        {
            "name": "Taxable",
            "type": "taxable",
            "cash_balance": 1000,
            "positions": [
                {
                    "symbol": "VTI",
                    "quantity": 10,
                    "instrument": {
                        "current_price": 200,
                        "allocation_asset_class": {"equity": 100},
                        "allocation_regions": {"north_america": 100},
                        "allocation_sectors": {"technology": 30, "healthcare": 70},
                    },
                },
                {
                    "symbol": "BND",
                    "quantity": 10,
                    "instrument": {
                        "current_price": 100,
                        "allocation_asset_class": {"fixed_income": 100},
                        "allocation_regions": {"north_america": 50, "europe": 50},
                        "allocation_sectors": {"treasury": 100},
                    },
                },
            ],
        }
    ]
}


def test_totals_and_percentages():
    text = analyze_portfolio(PORTFOLIO)

    assert "Total Value: $4,000.00" in text
    assert "Number of Positions: 2" in text
    assert "Taxable (taxable): $4,000.00 (100.0%)" in text
    assert "VTI: $2,000.00 (50.0%)" in text


def test_allocations_are_aggregated_by_value():
    text = analyze_portfolio(PORTFOLIO)

    assert "equity: $2,000.00" in text
    assert "fixed_income: $1,000.00" in text
    assert "cash: $1,000.00" in text
    assert "north_america: $2,500.00" in text
    assert "europe: $500.00" in text
    assert "healthcare: $1,400.00" in text


def test_missing_cash_and_price_do_not_crash():
    portfolio = {
        "accounts": [
            {
                "name": "A",
                "type": "other",
                "cash_balance": None,
                "positions": [{"symbol": "X", "quantity": 5, "instrument": {"current_price": None}}],
            }
        ]
    }
    # Missing price falls back to 1.0 per share
    assert "Total Value: $5.00" in analyze_portfolio(portfolio)


def test_empty_portfolio():
    assert "Total Value: $0.00" in analyze_portfolio({"accounts": []})
