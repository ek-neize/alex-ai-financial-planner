"""Unit tests for the retirement agent's deterministic calculations (no AWS needed)."""

import random

import pytest

from agent import (
    calculate_asset_allocation,
    calculate_portfolio_value,
    generate_projections,
    run_monte_carlo_simulation,
)

ALL_EQUITY = {"equity": 1.0, "bonds": 0.0, "real_estate": 0.0, "commodities": 0.0, "cash": 0.0}
ALL_CASH = {"equity": 0.0, "bonds": 0.0, "real_estate": 0.0, "commodities": 0.0, "cash": 1.0}


@pytest.fixture
def portfolio():
    return {
        "accounts": [
            {
                "cash_balance": 1000,
                "positions": [
                    {
                        "quantity": 10,
                        "instrument": {
                            "current_price": 100,
                            "allocation_asset_class": {"equity": 100},
                        },
                    },
                    {
                        "quantity": 20,
                        "instrument": {
                            "current_price": 50,
                            "allocation_asset_class": {"equity": 40, "fixed_income": 60},
                        },
                    },
                ],
            }
        ]
    }


def test_portfolio_value_includes_cash_and_positions(portfolio):
    assert calculate_portfolio_value(portfolio) == pytest.approx(1000 + 10 * 100 + 20 * 50)


def test_portfolio_value_of_empty_portfolio_is_zero():
    assert calculate_portfolio_value({"accounts": []}) == 0


def test_asset_allocation_weights_by_value(portfolio):
    allocation = calculate_asset_allocation(portfolio)
    # total 3000: cash 1000, equity 1000 + 400, bonds 600
    assert allocation["cash"] == pytest.approx(1000 / 3000)
    assert allocation["equity"] == pytest.approx(1400 / 3000)
    assert allocation["bonds"] == pytest.approx(600 / 3000)
    assert sum(allocation.values()) == pytest.approx(1.0)


def test_asset_allocation_of_empty_portfolio_is_all_zero():
    assert set(calculate_asset_allocation({"accounts": []}).values()) == {0}


def test_monte_carlo_results_are_internally_consistent():
    random.seed(42)
    result = run_monte_carlo_simulation(500_000, 20, 40_000, ALL_EQUITY, num_simulations=200)

    assert 0 <= result["success_rate"] <= 100
    assert result["percentile_10"] <= result["median_final_value"] <= result["percentile_90"]
    assert 0 <= result["average_years_lasted"] <= 30


def test_monte_carlo_huge_portfolio_always_succeeds():
    random.seed(0)
    result = run_monte_carlo_simulation(100_000_000, 10, 1_000, ALL_CASH, num_simulations=50)
    assert result["success_rate"] == 100.0


def test_monte_carlo_empty_portfolio_with_large_income_always_fails():
    random.seed(0)
    result = run_monte_carlo_simulation(0, 0, 1_000_000, ALL_CASH, num_simulations=50)
    assert result["success_rate"] == 0.0


def test_monte_carlo_expected_value_is_deterministic():
    # All cash returns 2% a year, plus a 10,000 annual contribution
    result = run_monte_carlo_simulation(100_000, 1, 10_000, ALL_CASH, num_simulations=10)
    assert result["expected_value_at_retirement"] == pytest.approx(100_000 * 1.02 + 10_000)


def test_projections_start_at_current_age_and_change_phase():
    projections = generate_projections(100_000, 10, ALL_EQUITY, current_age=50)

    assert projections[0]["age"] == 50
    assert projections[0]["portfolio_value"] == 100_000
    phases = [p["phase"] for p in projections]
    assert phases[0] == "accumulation"
    assert "retirement" in phases
    # Phases never go back from retirement to accumulation
    assert phases == sorted(phases, key=lambda p: p == "retirement")
