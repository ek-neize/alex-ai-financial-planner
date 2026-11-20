# Load test results: November 2026

Load tests of the agent pipeline (SQS → Planner → Reporter / Charter / Retirement, plus the Tagger when needed), run with [`load_test.py`](load_test.py) against a full deployment in `us-east-1`. Model: Amazon Nova Pro (cross-region inference profile). Aurora Serverless v2 at 0.5–1 ACU. Every portfolio has 6 positions across 1–3 accounts.

## Summary

The first burst of 10 concurrent jobs **failed its latency target (p95 431s)**, and 15 sub-agent runs hit the 300s Lambda timeout. The load test exposed two bugs, plus a fixed sleep that added ~30s to every job. After fixing them, **a 25-job burst completed 25/25 with p95 71s, and throughput went from 1.2 to 18.4 jobs/minute**.

| Run | Jobs (burst) | Success | p50 end-to-end | p95 end-to-end | Throughput | Sub-agent errors | Bedrock cost/job |
|---|---|---|---|---|---|---|---|
| Baseline | 3 | 3/3 | 114s | 115s | 1.5/min | 0 | $0.02 |
| Before fixes | 10 | 10/10* | 109s | **431s** ❌ | 1.2/min | **15 timeouts** | $0.03 |
| After fixes | 10 | 10/10 | 69s | 75s | 7.4/min | 0 | $0.02 |
| After fixes | 25 | **25/25** | 67s | **71s** | **18.4/min** | 0 | $0.02 |

\* All 10 jobs were marked "completed" even though sub-agents had failed. The load test now checks each job actually has a report, charts and a retirement projection, and counts it as "incomplete" otherwise.

## What the load test found

### 1. Agents hung on every warm Lambda invocation

- **Symptom:** sub-agents hit the 300s timeout under load.
- **Evidence:** every timeout was in a *warm* container, and no cold start timed out. Reporter: 12/12 cold invocations succeeded, 11/11 warm ones hung. Reproduced with no load at all: after one job, a direct invoke of the warm reporter hung for 300s.
- **Cause:** each invocation configured logfire, instrumented the Agents SDK and created a Langfuse client, then called `shutdown()` at the end. Shutdown is final for the process-wide tracing pipeline, and the next warm invocation hung on top of it.
- **Fix:** set up observability once per container and only `flush()` per invocation ([commit](https://github.com/ek-neize/alex-ai-financial-planner/commit/fc193b1)). After the fix, three back-to-back invocations took 30s (cold), 11s and 14s (warm).

### 2. The planner duplicated slow sub-agent runs

- **Evidence:** a hung reporter was re-invoked at 08:25:16, 08:26:17, 08:27:18, 08:28:18 and 08:29:20, about 60s apart. The reporter ran 20 times for 10 jobs.
- **Cause:** boto3's Lambda client defaults to a 60s read timeout and retries on timeout. A synchronous invoke still running after 60s was invoked *again*, duplicating its Bedrock spend and database writes. Separately, a timed-out sub-agent returns HTTP 200 with `FunctionError` set, which the planner logged as "completed successfully".
- **Fix:** `read_timeout=310` (above the 300s sub-agent timeout), `total_max_attempts=1`, and treat `FunctionError` as a failure. A unit test caught that `max_attempts: 1` would still have allowed one retry, because botocore counts retries in that setting. After the fix, each agent ran exactly once per job (25/25/25/25).

### 3. A fixed 10–15s sleep after every trace flush

- Each Lambda slept after flushing traces: 15s in the planner and 10s in each sub-agent. Because the planner calls the sub-agents one after another, that's ~30s of every job's processing time, plus ~45s of billed Lambda time per job.
- `flush()` already blocks until spans are exported, so the sleep was removed. Langfuse traces still arrive with the full span tree (verified through the Langfuse API).
- Median processing time dropped from 97s to 57s.

## Capacity and remaining bottlenecks

At 25 concurrent jobs there were no Lambda throttles, no Bedrock throttles and nothing in the DLQ. Latency was flat between 10 and 25 jobs. The limits that will bind next:

| Resource | Limit | Observed at 25 jobs | Outlook |
|---|---|---|---|
| SageMaker Serverless (embeddings) | 10 concurrent requests | **1 of 25 reporters throttled**; it silently wrote its report without market research | First bottleneck. Raise `max_concurrency`, or retry with backoff in `get_market_insights` |
| Bedrock Nova Pro (cross-region) | 500 requests/min, 2M tokens/min | ~120 requests/min, ~190k tokens/min, 0 throttles | Roughly 100 concurrent jobs before throttling (estimate) |
| Aurora Serverless v2 | max 1 ACU (tfvars) | Reached 1 ACU, with no visible latency impact | Raise `max_capacity` before testing beyond ~25 concurrent jobs |
| Lambda concurrency | 400 (account) | Peak 25 planners + up to 25 per sub-agent | Not a constraint at this scale |
| Polygon.io (free tier) | 5 requests/min | Price lookups failed on every job | Non-fatal (stored prices are used); needs a paid plan or a shared cache |

## Next optimisation

Processing is ~57s because the planner calls the reporter (~22s), charter (~10s) and retirement (~10s) agents **one after another**. Running them in parallel could bring processing to ~35s. That change is to the agent's tool-calling behaviour, so it should be its own experiment, measured with this load test.

## Reproduce

```bash
cd backend/loadtest
uv run load_test.py --jobs 25          # burst
uv run load_test.py --jobs 50 --rate 10   # 10 jobs/minute
```

Needs `terraform/2_sagemaker`, `5_database` and `6_agents` deployed. Reports are written to `reports/` (gitignored).
