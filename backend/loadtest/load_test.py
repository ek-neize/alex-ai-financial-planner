#!/usr/bin/env python3
"""
Load test for the Alex agent pipeline (SQS -> Planner -> Tagger/Reporter/Charter/Retirement).

Creates N test users with portfolios, enqueues an analysis job for each (as a
burst or at a steady rate), tracks every job to completion, pulls CloudWatch
metrics for the test window (Lambda, SQS, Aurora, Bedrock tokens and
throttles), and writes a Markdown + JSON report with pass/fail against the
targets. Test users are always deleted afterwards (cascading to their
accounts, positions and jobs).

Needs terraform/5_database and terraform/6_agents deployed. Every job makes
real Bedrock calls, so it costs money: the report shows the measured cost per job.

Usage (from backend/loadtest):
    uv run load_test.py --jobs 5                 # small burst
    uv run load_test.py --jobs 20 --rate 10      # 10 jobs/minute
    uv run load_test.py --jobs 30 --slo-p95 240  # stricter latency target
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv

from stats import (
    TERMINAL,
    JobResult,
    Slo,
    arrival_offsets,
    bedrock_cost,
    classify,
    evaluate_slo,
    render_markdown,
    summarize,
)

load_dotenv(override=False)

from src import Database  # noqa: E402  (after load_dotenv so the client sees .env)

REGION = os.getenv("DEFAULT_AWS_REGION", "us-east-1")
QUEUE_NAME = "alex-analysis-jobs"
DLQ_NAME = "alex-analysis-jobs-dlq"
CLUSTER_ID = "alex-aurora-cluster"
AGENTS = ["planner", "tagger", "reporter", "charter", "retirement"]
USER_PREFIX = "loadtest_"
REPORTS_DIR = Path(__file__).parent / "reports"


def utcnow() -> datetime:
    """Naive UTC, matching the timestamps the agents write to the jobs table."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(value) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def preflight(db: Database) -> dict:
    """Check the deployed pieces exist; return queue URLs and the instruments to use."""
    sqs = boto3.client("sqs", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    problems = []

    try:
        queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    except Exception:
        queue_url = None
        problems.append(f"SQS queue {QUEUE_NAME} not found (deploy terraform/6_agents)")
    try:
        dlq_url = sqs.get_queue_url(QueueName=DLQ_NAME)["QueueUrl"]
    except Exception:
        dlq_url = None

    model_id = None
    for agent in AGENTS:
        try:
            config = lam.get_function_configuration(FunctionName=f"alex-{agent}")
            model_id = model_id or config.get("Environment", {}).get("Variables", {}).get("BEDROCK_MODEL_ID")
        except Exception:
            problems.append(f"Lambda alex-{agent} not found (deploy terraform/6_agents)")

    symbols = []
    try:
        rows = db.query_raw("SELECT symbol FROM instruments WHERE current_price > 0 ORDER BY symbol")
        symbols = [r["symbol"] for r in rows]
        if len(symbols) < 5:
            problems.append(f"Only {len(symbols)} priced instruments; run seed_data.py")
    except Exception as e:
        problems.append(f"Database not reachable: {e}")

    if problems:
        print("❌ Preflight failed:")
        for p in problems:
            print(f"   - {p}")
        sys.exit(1)

    print(f"✅ Preflight: queue, 5 agent Lambdas, {len(symbols)} instruments. Model: {model_id}")
    return {"queue_url": queue_url, "dlq_url": dlq_url, "symbols": symbols, "model_id": model_id}


# --------------------------------------------------------------------------
# Test data
# --------------------------------------------------------------------------


def create_users(db: Database, run_id: str, jobs: int, positions: int, symbols: list[str]) -> list[dict]:
    """One user per job, each with 1-3 accounts and a random portfolio of seeded ETFs."""
    rng = random.Random(run_id)  # reproducible per run
    users = []
    for i in range(jobs):
        user_id = f"{USER_PREFIX}{run_id}_{i:03d}"
        db.users.create_user(
            clerk_user_id=user_id,
            display_name=f"Load test {i}",
            years_until_retirement=rng.randint(5, 35),
            target_retirement_income=rng.randrange(40_000, 150_000, 5_000),
        )
        accounts = [
            db.accounts.create_account(
                clerk_user_id=user_id,
                account_name=name,
                account_purpose="load test",
                cash_balance=float(rng.randrange(0, 20_000, 500)),
            )
            for name in ["401k", "Roth IRA", "Taxable"][: rng.randint(1, 3)]
        ]
        for symbol in rng.sample(symbols, min(positions, len(symbols))):
            db.positions.add_position(rng.choice(accounts), symbol, float(rng.randrange(5, 200)))

        job_id = db.jobs.create_job(user_id, "portfolio_analysis", {"load_test": run_id})
        users.append({"user_id": user_id, "job_id": job_id})
    return users


def cleanup(db: Database, run_id: str):
    """Delete this run's users; accounts, positions and jobs cascade."""
    db.execute_raw(
        "DELETE FROM users WHERE clerk_user_id LIKE :prefix",
        [{"name": "prefix", "value": {"stringValue": f"{USER_PREFIX}{run_id}_%"}}],
    )


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def enqueue_all(queue_url: str, users: list[dict], rate: float) -> dict[str, datetime]:
    sqs = boto3.client("sqs", region_name=REGION)
    enqueued = {}
    start = time.monotonic()
    for user, offset in zip(users, arrival_offsets(len(users), rate)):
        delay = offset - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"job_id": user["job_id"]}))
        enqueued[user["job_id"]] = utcnow()
        print(f"   → job {len(enqueued)}/{len(users)} enqueued", end="\r")
    print()
    return enqueued


def poll_jobs(db: Database, run_id: str, enqueued: dict[str, datetime], timeout: float) -> list[JobResult]:
    deadline = time.monotonic() + timeout
    while True:
        rows = db.query_raw(
            """SELECT j.id, j.status, j.started_at, j.completed_at, j.error_message,
                      j.report_payload IS NOT NULL AS has_report,
                      j.charts_payload IS NOT NULL AS has_charts,
                      j.retirement_payload IS NOT NULL AS has_retirement
               FROM jobs j WHERE j.clerk_user_id LIKE :prefix""",
            [{"name": "prefix", "value": {"stringValue": f"{USER_PREFIX}{run_id}_%"}}],
        )
        results = []
        for r in rows:
            if r["id"] not in enqueued:
                continue
            status, missing = classify(r["status"], r["has_report"], r["has_charts"], r["has_retirement"])
            results.append(JobResult(
                job_id=r["id"],
                status=status,
                enqueued_at=enqueued[r["id"]],
                started_at=parse_ts(r.get("started_at")),
                completed_at=parse_ts(r.get("completed_at")),
                error=r.get("error_message") or missing,
            ))
        counts = {s: sum(r.status == s for r in results) for s in ["pending", "running", "completed", "incomplete", "failed"]}
        print(f"   {datetime.now():%H:%M:%S}  " + "  ".join(f"{k}: {v}" for k, v in counts.items()), flush=True)

        if all(r.status in TERMINAL for r in results):
            return results
        if time.monotonic() > deadline:
            for r in results:
                if r.status not in TERMINAL:
                    r.status = "timeout"
            return results
        time.sleep(10)


# --------------------------------------------------------------------------
# CloudWatch
# --------------------------------------------------------------------------


def collect_metrics(start: datetime, end: datetime, model_id: str | None) -> dict:
    cw = boto3.client("cloudwatch", region_name=REGION)
    start, end = start - timedelta(minutes=2), end + timedelta(minutes=2)
    queries, labels = [], []

    def add(namespace, metric, dims, stat, label):
        queries.append({
            "Id": f"m{len(queries)}",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric,
                           "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
                "Period": 60,
                "Stat": stat,
            },
        })
        labels.append((label, stat))

    for agent in AGENTS:
        fn = {"FunctionName": f"alex-{agent}"}
        for metric in ["Invocations", "Errors", "Throttles"]:
            add("AWS/Lambda", metric, fn, "Sum", (agent, metric))
        add("AWS/Lambda", "ConcurrentExecutions", fn, "Maximum", (agent, "ConcurrentExecutions"))
        add("AWS/Lambda", "Duration", fn, "p95", (agent, "DurationP95"))
        add("AWS/Lambda", "Duration", fn, "Maximum", (agent, "DurationMax"))

    add("AWS/SQS", "ApproximateAgeOfOldestMessage", {"QueueName": QUEUE_NAME}, "Maximum",
        ("_", "Max SQS message age (s)"))
    add("AWS/SQS", "ApproximateNumberOfMessagesVisible", {"QueueName": DLQ_NAME}, "Maximum",
        ("_", "Messages in DLQ"))
    add("AWS/RDS", "ServerlessDatabaseCapacity", {"DBClusterIdentifier": CLUSTER_ID}, "Maximum",
        ("_", "Max Aurora capacity (ACU)"))

    if model_id:
        for metric in ["Invocations", "InvocationThrottles", "InvocationClientErrors", "InputTokenCount", "OutputTokenCount"]:
            add("AWS/Bedrock", metric, {"ModelId": model_id}, "Sum", ("_bedrock", metric))

    data = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)["MetricDataResults"]

    metrics: dict = {"lambda": {a: {} for a in AGENTS}}
    bedrock: dict = {}
    for result, (label, stat) in zip(data, labels):
        values = result["Values"]
        if not values:
            continue
        value = max(values) if stat in ("Maximum", "p95") else sum(values)
        owner, name = label
        if owner in metrics["lambda"]:
            metrics["lambda"][owner][name] = value
        elif owner == "_bedrock":
            bedrock[name] = value
        else:
            metrics[name] = value

    # Degradations that don't fail a job, e.g. the reporter writing its report without
    # market research because the embedding endpoint throttled it
    logs = boto3.client("logs", region_name=REGION)
    try:
        count, token = 0, None
        while True:
            kwargs = {"logGroupName": "/aws/lambda/alex-reporter", "filterPattern": '"Could not retrieve market insights"',
                      "startTime": int(start.replace(tzinfo=timezone.utc).timestamp() * 1000),
                      "endTime": int(end.replace(tzinfo=timezone.utc).timestamp() * 1000)}
            if token:
                kwargs["nextToken"] = token
            page = logs.filter_log_events(**kwargs)
            count += len(page.get("events", []))
            token = page.get("nextToken")
            if not token:
                break
        metrics["Reports without market insights"] = count
    except Exception as e:
        print(f"   ⚠️  Couldn't count market insight failures: {e}")

    if bedrock:
        metrics["Bedrock invocations"] = bedrock.get("Invocations", 0)
        metrics["Bedrock throttles"] = bedrock.get("InvocationThrottles", 0)
        metrics["Bedrock client errors"] = bedrock.get("InvocationClientErrors", 0)
        metrics["Bedrock input tokens"] = bedrock.get("InputTokenCount", 0)
        metrics["Bedrock output tokens"] = bedrock.get("OutputTokenCount", 0)
    return metrics


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", type=int, default=5, help="number of analysis jobs (default 5)")
    parser.add_argument("--rate", type=float, default=0, help="jobs per minute; 0 = burst (default)")
    parser.add_argument("--positions", type=int, default=6, help="positions per portfolio (default 6)")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait after the last enqueue")
    parser.add_argument("--slo-success", type=float, default=0.95, help="minimum success rate (default 0.95)")
    parser.add_argument("--slo-p95", type=float, default=300, help="max p95 end-to-end seconds (default 300)")
    parser.add_argument("--metrics-delay", type=float, default=180,
                        help="seconds to wait for CloudWatch metrics to land (default 180)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--keep-data", action="store_true", help="don't delete test users afterwards")
    args = parser.parse_args()

    run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
    db = Database()
    pre = preflight(db)

    pattern = "all at once" if args.rate <= 0 else f"at {args.rate:g}/minute"
    print(f"\n🧪 Load test {run_id}: {args.jobs} jobs {pattern}, {args.positions} positions each")
    print("   Every job makes real Bedrock calls (typically a few cents each).")
    if not args.yes and input("   Continue? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    config = {"run_id": run_id, "jobs": args.jobs, "rate": args.rate, "positions": args.positions,
              "model_id": pre["model_id"], "started_at": f"{utcnow():%Y-%m-%d %H:%M:%S}"}
    try:
        print("\n👥 Creating test users and portfolios...")
        users = create_users(db, run_id, args.jobs, args.positions, pre["symbols"])

        print("🚀 Enqueuing jobs...")
        started = utcnow()
        t0 = time.monotonic()
        enqueued = enqueue_all(pre["queue_url"], users, args.rate)

        print("⏳ Waiting for jobs...")
        results = poll_jobs(db, run_id, enqueued, args.timeout)
        wall_clock = time.monotonic() - t0
        ended = utcnow()
    finally:
        if args.keep_data:
            print(f"\n⚠️  Keeping test data (users {USER_PREFIX}{run_id}_*)")
        else:
            print("\n🧹 Deleting test users (accounts, positions and jobs cascade)...")
            cleanup(db, run_id)

    summary = summarize(results, wall_clock)

    print(f"📈 Waiting {args.metrics_delay:.0f}s for CloudWatch metrics...")
    time.sleep(args.metrics_delay)
    try:
        metrics = collect_metrics(started, ended, pre["model_id"])
    except Exception as e:
        print(f"   ⚠️  Couldn't read CloudWatch metrics: {e}")
        metrics = {}

    if "Bedrock input tokens" in metrics:
        cost = bedrock_cost(metrics["Bedrock input tokens"], metrics["Bedrock output tokens"])
        metrics["Bedrock cost (USD, Nova Pro on-demand)"] = round(cost, 4)
        completed = summary["by_status"].get("completed", 0)
        if completed:
            metrics["Bedrock cost per completed job (USD)"] = round(cost / completed, 4)

    slo = evaluate_slo(summary, Slo(args.slo_success, args.slo_p95), metrics.get("Bedrock throttles"))
    report = render_markdown(config, summary, slo, metrics)

    REPORTS_DIR.mkdir(exist_ok=True)
    md_path = REPORTS_DIR / f"{run_id}.md"
    md_path.write_text(report, encoding="utf-8")
    (REPORTS_DIR / f"{run_id}.json").write_text(
        json.dumps({"config": config, "summary": summary, "metrics": metrics,
                    "slo": {"passed": slo.passed, "checks": slo.checks}}, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n" + report)
    print(f"📝 Report saved to {md_path}")
    return 0 if slo.passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
