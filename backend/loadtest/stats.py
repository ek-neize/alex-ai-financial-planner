"""Pure functions for the load test: arrival schedule, latency statistics,
SLO evaluation, cost and report rendering. No AWS access, so they're unit-tested."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

# Amazon Nova Pro on-demand pricing, USD per 1,000 tokens (us-east-1)
NOVA_PRO_INPUT_PER_1K = 0.0008
NOVA_PRO_OUTPUT_PER_1K = 0.0032

TERMINAL = {"completed", "failed"}


def arrival_offsets(jobs: int, rate_per_minute: float) -> list[float]:
    """Seconds after start at which each job is enqueued.

    rate_per_minute <= 0 means a burst: every job at t=0.
    """
    if jobs < 0:
        raise ValueError("jobs must be >= 0")
    if rate_per_minute <= 0:
        return [0.0] * jobs
    interval = 60.0 / rate_per_minute
    return [i * interval for i in range(jobs)]


def percentile(values: list[float], pct: float) -> float | None:
    """Percentile with linear interpolation (same as numpy's default). None if empty."""
    if not values:
        return None
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass
class JobResult:
    job_id: str
    status: str  # pending | running | completed | failed | timeout
    enqueued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

    @property
    def queue_wait(self) -> float | None:
        """Seconds from enqueue until the planner picked the job up."""
        return _seconds(self.enqueued_at, self.started_at)

    @property
    def processing(self) -> float | None:
        """Seconds the planner spent on the job (including all sub-agents)."""
        return _seconds(self.started_at, self.completed_at)

    @property
    def end_to_end(self) -> float | None:
        """Seconds from enqueue until results were saved (completed jobs only)."""
        if self.status != "completed":
            return None
        return _seconds(self.enqueued_at, self.completed_at)


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _latency_summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def summarize(results: list[JobResult], wall_clock_seconds: float) -> dict:
    total = len(results)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    completed = by_status.get("completed", 0)

    return {
        "jobs": total,
        "by_status": by_status,
        "success_rate": completed / total if total else 0.0,
        "wall_clock_seconds": wall_clock_seconds,
        "throughput_jobs_per_minute": completed / (wall_clock_seconds / 60) if wall_clock_seconds > 0 else 0.0,
        "end_to_end": _latency_summary([v for r in results if (v := r.end_to_end) is not None]),
        "queue_wait": _latency_summary([v for r in results if (v := r.queue_wait) is not None]),
        "processing": _latency_summary(
            [v for r in results if r.status == "completed" and (v := r.processing) is not None]
        ),
        "errors": sorted({r.error for r in results if r.error})[:10],
    }


@dataclass
class Slo:
    min_success_rate: float = 0.95
    max_p95_end_to_end: float = 300.0  # seconds


@dataclass
class SloResult:
    passed: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, passed, detail)


def evaluate_slo(summary: dict, slo: Slo, bedrock_throttles: float | None = None) -> SloResult:
    checks = []

    rate = summary["success_rate"]
    checks.append((
        "Success rate",
        rate >= slo.min_success_rate,
        f"{rate:.1%} (target ≥ {slo.min_success_rate:.0%})",
    ))

    p95 = summary["end_to_end"]["p95"]
    checks.append((
        "p95 end-to-end latency",
        p95 is not None and p95 <= slo.max_p95_end_to_end,
        f"{_fmt_s(p95)} (target ≤ {slo.max_p95_end_to_end:.0f}s)",
    ))

    if bedrock_throttles is not None:
        # Informational: throttles are retried, so they don't fail the run on their own
        checks.append(("Bedrock throttles (info)", True, f"{bedrock_throttles:.0f}"))

    return SloResult(passed=all(ok for _, ok, _ in checks), checks=checks)


def bedrock_cost(input_tokens: float, output_tokens: float) -> float:
    return input_tokens / 1000 * NOVA_PRO_INPUT_PER_1K + output_tokens / 1000 * NOVA_PRO_OUTPUT_PER_1K


def _fmt_s(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def render_markdown(config: dict, summary: dict, slo: SloResult, metrics: dict) -> str:
    lines = [
        f"# Load test {config['run_id']}",
        "",
        f"**Result: {'✅ PASS' if slo.passed else '❌ FAIL'}**",
        "",
        "## Configuration",
        "",
        f"- Jobs: {config['jobs']}, "
        + ("burst (all at once)" if config["rate"] <= 0 else f"{config['rate']:g} jobs/minute"),
        f"- Positions per portfolio: {config['positions']}",
        f"- Started: {config['started_at']} UTC, wall clock {summary['wall_clock_seconds']:.0f}s",
        "",
        "## Targets",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
        *[f"| {name} | {'✅' if ok else '❌'} | {detail} |" for name, ok, detail in slo.checks],
        "",
        "## Jobs",
        "",
        f"- Outcomes: {', '.join(f'{k}: {v}' for k, v in sorted(summary['by_status'].items()))}",
        f"- Success rate: {summary['success_rate']:.1%}",
        f"- Throughput: {summary['throughput_jobs_per_minute']:.2f} completed jobs/minute",
        "",
        "| Latency | n | p50 | p90 | p95 | max |",
        "|---|---|---|---|---|---|",
    ]
    for label, key in [("End-to-end", "end_to_end"), ("Queue wait", "queue_wait"), ("Processing", "processing")]:
        s = summary[key]
        lines.append(
            f"| {label} | {s['count']} | {_fmt_s(s['p50'])} | {_fmt_s(s['p90'])} | {_fmt_s(s['p95'])} | {_fmt_s(s['max'])} |"
        )

    if summary["errors"]:
        lines += ["", "**Errors:**", "", *[f"- `{e[:200]}`" for e in summary["errors"]]]

    if metrics:
        lines += ["", "## AWS metrics during the test", ""]
        functions = metrics.get("lambda", {})
        if functions:
            lines += [
                "| Lambda | Invocations | Errors | Throttles | Max concurrency | p95 duration | Max duration |",
                "|---|---|---|---|---|---|---|",
            ]
            for name, m in functions.items():
                lines.append(
                    f"| {name} | {_n(m.get('Invocations'))} | {_n(m.get('Errors'))} | {_n(m.get('Throttles'))} "
                    f"| {_n(m.get('ConcurrentExecutions'))} | {_ms(m.get('DurationP95'))} | {_ms(m.get('DurationMax'))} |"
                )
        other = {k: v for k, v in metrics.items() if k != "lambda"}
        if other:
            lines += ["", *[f"- {k}: {_n(v) if isinstance(v, (int, float)) else v}" for k, v in other.items()]]

    return "\n".join(lines) + "\n"


def _n(value) -> str:
    if value is None:
        return "–"
    return f"{value:,.0f}" if float(value).is_integer() or abs(value) >= 100 else f"{value:,.2f}"


def _ms(value) -> str:
    return "–" if value is None else f"{value / 1000:.1f}s"
