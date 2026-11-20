"""The reporter's SageMaker client must ride out endpoint throttling (no AWS needed)."""

import agent


def test_sagemaker_client_retries_throttling_with_backoff():
    retries = agent.SAGEMAKER_CONFIG.retries
    # 'standard' mode: exponential backoff with jitter, retries ThrottlingException
    assert retries["mode"] == "standard"
    # counts the first attempt too ('max_attempts' would count retries only)
    assert retries["total_max_attempts"] >= 5
