"""Unit tests for the load test's statistics, SLO checks and report (no AWS needed)."""

from datetime import datetime, timedelta

import pytest

from stats import (
    JobResult,
    Slo,
    arrival_offsets,
    bedrock_cost,
    evaluate_slo,
    percentile,
    render_markdown,
    summarize,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def job(status="completed", wait=10, processing=60, error=None, job_id="j"):
    started = T0 + timedelta(seconds=wait) if status != "pending" else None
    completed = started + timedelta(seconds=processing) if status in ("completed", "failed") else None
    return JobResult(job_id, status, T0, started, completed, error)


# --------------------------------------------------------------------------
# arrival_offsets / percentile
# --------------------------------------------------------------------------


def test_burst_enqueues_everything_at_once():
    assert arrival_offsets(4, 0) == [0, 0, 0, 0]


def test_rate_spaces_jobs_evenly():
    assert arrival_offsets(4, 30) == [0, 2, 4, 6]  # 30/min = every 2s


def test_negative_job_count_rejected():
    with pytest.raises(ValueError):
        arrival_offsets(-1, 0)


def test_percentile_interpolates_like_numpy():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 50) == 5.5
    assert percentile(values, 90) == pytest.approx(9.1)
    assert percentile(values, 0) == 1
    assert percentile(values, 100) == 10
    assert percentile([42], 95) == 42


def test_percentile_of_nothing_is_none():
    assert percentile([], 95) is None


# --------------------------------------------------------------------------
# JobResult / summarize
# --------------------------------------------------------------------------


def test_job_timings():
    j = job(wait=12, processing=90)
    assert j.queue_wait == 12
    assert j.processing == 90
    assert j.end_to_end == 102


def test_failed_or_unfinished_jobs_have_no_end_to_end_latency():
    assert job("failed").end_to_end is None
    assert job("running").end_to_end is None
    assert job("pending").queue_wait is None


def test_summarize():
    results = [
        job(wait=5, processing=60),
        job(wait=10, processing=80),
        job(wait=20, processing=100),
        job("failed", error="Bedrock throttled"),
        job("timeout"),
    ]
    s = summarize(results, wall_clock_seconds=120)

    assert s["jobs"] == 5
    assert s["by_status"] == {"completed": 3, "failed": 1, "timeout": 1}
    assert s["success_rate"] == pytest.approx(0.6)
    assert s["throughput_jobs_per_minute"] == pytest.approx(1.5)  # 3 completed in 2 minutes
    assert s["end_to_end"]["count"] == 3
    assert s["end_to_end"]["p50"] == 90
    assert s["end_to_end"]["max"] == 120
    assert s["processing"]["count"] == 3  # failed job's processing time is excluded
    assert s["errors"] == ["Bedrock throttled"]


def test_summarize_with_no_jobs():
    s = summarize([], wall_clock_seconds=0)
    assert s["success_rate"] == 0
    assert s["end_to_end"]["p95"] is None


# --------------------------------------------------------------------------
# evaluate_slo
# --------------------------------------------------------------------------


def test_slo_passes_when_targets_met():
    s = summarize([job(processing=60) for _ in range(20)], 300)
    result = evaluate_slo(s, Slo(min_success_rate=0.95, max_p95_end_to_end=120))
    assert result.passed


def test_slo_fails_on_low_success_rate():
    s = summarize([job() for _ in range(9)] + [job("failed")], 300)
    result = evaluate_slo(s, Slo(min_success_rate=0.95, max_p95_end_to_end=600))
    assert not result.passed
    assert [name for name, ok, _ in result.checks if not ok] == ["Success rate"]


def test_slo_fails_on_slow_p95():
    # 2 of 20 slow: p95 falls in the slow tail (1 of 20 would interpolate to ~92s)
    s = summarize([job(processing=60) for _ in range(18)] + [job(processing=500)] * 2, 600)
    result = evaluate_slo(s, Slo(min_success_rate=0.9, max_p95_end_to_end=120))
    assert not result.passed
    assert [name for name, ok, _ in result.checks if not ok] == ["p95 end-to-end latency"]


def test_slo_fails_when_nothing_completed():
    s = summarize([job("failed")], 60)
    assert not evaluate_slo(s, Slo()).passed


def test_bedrock_throttles_are_informational():
    s = summarize([job() for _ in range(10)], 60)
    result = evaluate_slo(s, Slo(max_p95_end_to_end=600), bedrock_throttles=12)
    assert result.passed
    assert ("Bedrock throttles (info)", True, "12") in result.checks


# --------------------------------------------------------------------------
# cost and report
# --------------------------------------------------------------------------


def test_bedrock_cost_uses_nova_pro_pricing():
    # $0.80 per 1M input tokens, $3.20 per 1M output tokens
    assert bedrock_cost(1_000_000, 1_000_000) == pytest.approx(4.0)


def test_report_contains_result_targets_and_metrics():
    s = summarize([job(), job("failed", error="boom")], 120)
    slo = evaluate_slo(s, Slo())
    config = {"run_id": "r1", "jobs": 2, "rate": 0, "positions": 6, "started_at": "2026-01-01 12:00:00"}
    metrics = {"lambda": {"planner": {"Invocations": 2, "Throttles": 0, "DurationP95": 81000}},
               "Bedrock throttles": 3}

    md = render_markdown(config, s, slo, metrics)

    assert "❌ FAIL" in md
    assert "burst (all at once)" in md
    assert "| planner | 2 | – | 0 | – | 81.0s | – |" in md
    assert "Bedrock throttles: 3" in md
    assert "`boom`" in md


# --------------------------------------------------------------------------
# classify: a "completed" job must actually contain its results
# --------------------------------------------------------------------------

from stats import classify  # noqa: E402


def test_completed_job_with_all_results_is_completed():
    assert classify("completed", True, True, True) == ("completed", None)


def test_completed_job_missing_results_is_incomplete():
    assert classify("completed", True, False, False) == ("incomplete", "completed without: charts, retirement")


def test_other_statuses_pass_through():
    assert classify("failed", False, False, False) == ("failed", None)
    assert classify("running", False, False, False) == ("running", None)


def test_incomplete_jobs_count_against_success_rate():
    results = [job(), job("incomplete", error="completed without: report")]
    assert summarize(results, 60)["success_rate"] == 0.5
