"""Unit tests for the reporter agent's portfolio metrics and prompt formatting (no AWS needed)."""

import pytest

from agent import calculate_portfolio_metrics, format_portfolio_for_analysis


@pytest.fixture
def portfolio():
    return {
        "accounts": [
            {
                "name": "401k",
                "cash_balance": 500,
                "positions": [
                    {"symbol": "SPY", "quantity": 10, "instrument": {"current_price": 400}},
                    {"symbol": "BND", "quantity": 5, "instrument": {"current_price": 70}},
                ],
            },
            {
                "name": "Roth IRA",
                "cash_balance": 0,
                "positions": [
                    {"symbol": "SPY", "quantity": 2, "instrument": {"current_price": 400}},
                    # No price: counted as a position but contributes no value
                    {"symbol": "NEW", "quantity": 3, "instrument": {}},
                ],
            },
        ]
    }


def test_metrics(portfolio):
    metrics = calculate_portfolio_metrics(portfolio)

    assert metrics["num_accounts"] == 2
    assert metrics["num_positions"] == 4
    assert metrics["unique_symbols"] == 3
    assert metrics["cash_balance"] == 500
    assert metrics["total_value"] == pytest.approx(500 + 10 * 400 + 5 * 70 + 2 * 400)


def test_metrics_of_empty_portfolio():
    metrics = calculate_portfolio_metrics({"accounts": []})
    assert metrics["total_value"] == 0
    assert metrics["unique_symbols"] == 0


def test_format_includes_accounts_holdings_and_user_profile(portfolio):
    text = format_portfolio_for_analysis(
        portfolio, {"years_until_retirement": 25, "target_retirement_income": 80000}
    )

    assert "2 accounts" in text
    assert "401k ($500.00 cash)" in text
    assert "SPY: 10.00 shares" in text
    assert "Years to retirement: 25" in text
    assert "$80,000/year" in text
