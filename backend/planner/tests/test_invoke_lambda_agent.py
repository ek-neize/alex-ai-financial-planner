"""Unit tests for how the planner calls sub-agent Lambdas (stubbed client, no AWS)."""

import io
import json

import pytest

import agent


class StubLambda:
    def __init__(self, payload, function_error=None):
        self.payload = payload
        self.function_error = function_error
        self.calls = 0

    def invoke(self, **_):
        self.calls += 1
        response = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.payload).encode())}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


@pytest.fixture
def stub(monkeypatch):
    def install(payload, function_error=None):
        client = StubLambda(payload, function_error)
        monkeypatch.setattr(agent, "lambda_client", client)
        monkeypatch.setattr(agent, "MOCK_LAMBDAS", False)
        return client

    return install


async def test_success_unwraps_lambda_body(stub):
    stub({"statusCode": 200, "body": json.dumps({"success": 1, "message": "Report stored"})})
    assert await agent.invoke_lambda_agent("Reporter", "alex-reporter", {"job_id": "j"}) == {
        "success": 1,
        "message": "Report stored",
    }


async def test_timed_out_sub_agent_is_an_error_not_a_success(stub):
    # What Lambda returns when the function hits its timeout: HTTP 200 + FunctionError
    stub({"errorType": "Sandbox.Timedout", "errorMessage": "Task timed out after 300.00 seconds"},
         function_error="Unhandled")
    result = await agent.invoke_lambda_agent("Reporter", "alex-reporter", {"job_id": "j"})
    assert result == {"error": "Reporter failed: Task timed out after 300.00 seconds"}


async def test_tool_reports_failure_to_the_planner_llm(stub):
    stub({"errorMessage": "boom"}, function_error="Unhandled")
    message = await agent.invoke_reporter_internal("j")
    assert message.startswith("Reporter agent failed")


def test_client_waits_for_full_sub_agent_timeout_and_never_retries():
    config = agent.lambda_client.meta.config
    assert config.read_timeout > 300  # sub-agent Lambda timeout
    # total_max_attempts counts the first call; "max_attempts" would mean 1 *retry*
    assert config.retries["total_max_attempts"] == 1


class SlowStubLambda(StubLambda):
    """Blocks like the real boto3 invoke() does while the sub-agent runs."""

    def __init__(self, seconds):
        super().__init__({"statusCode": 200, "body": json.dumps({"success": 1})})
        self.seconds = seconds

    def invoke(self, **kwargs):
        import time

        time.sleep(self.seconds)
        return super().invoke(**kwargs)


async def test_sub_agents_run_concurrently(monkeypatch):
    # The model issues invoke_reporter, invoke_charter and invoke_retirement in one
    # turn and the Agents SDK runs them concurrently, but a blocking boto3 call
    # inside an async tool freezes the event loop, so they ran one after another
    # (seen in Langfuse: three tool spans, total = sum of the three agents).
    import asyncio
    import time

    monkeypatch.setattr(agent, "lambda_client", SlowStubLambda(0.5))
    monkeypatch.setattr(agent, "MOCK_LAMBDAS", False)

    start = time.monotonic()
    await asyncio.gather(
        agent.invoke_reporter_internal("j"),
        agent.invoke_charter_internal("j"),
        agent.invoke_retirement_internal("j"),
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"took {elapsed:.2f}s: sub-agents ran sequentially (3 x 0.5s)"
