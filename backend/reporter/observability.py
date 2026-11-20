"""
Observability module for LangFuse integration.
Provides a simple context manager for setting up and flushing traces.
"""

import os
import logging
from contextlib import contextmanager

logger = logging.getLogger()

SERVICE_NAME = "alex_reporter_agent"

# Set up once per Lambda container and reused by warm invocations.
#
# Earlier versions configured logfire, instrumented the Agents SDK and created a
# Langfuse client on every invocation, then called shutdown() at the end. The
# next warm invocation in the same container then hung until the Lambda timed
# out (reproduced: a second invoke of a warm reporter always hit the 300s
# limit). Shutdown is final, so only flush() per invocation.
_langfuse_client = None
_setup_attempted = False


def _setup():
    """Configure logfire + Langfuse once per container. Returns the client or None."""
    global _langfuse_client, _setup_attempted
    if _setup_attempted:
        return _langfuse_client
    _setup_attempted = True

    try:
        logger.info("🔍 Observability: Setting up LangFuse...")

        import logfire
        from langfuse import Langfuse, get_client
        from langfuse.span_filter import is_default_export_span

        # Configure logfire to instrument OpenAI Agents SDK
        logfire.configure(
            service_name=SERVICE_NAME,
            send_to_logfire=False,  # Don't send to Logfire cloud
        )
        logfire.instrument_openai_agents()
        logger.info("✅ Observability: OpenAI Agents SDK instrumented")

        # Langfuse SDK v4+ only exports LLM-looking spans by default, which would drop
        # logfire's "Agent run" / "Function" spans and break the trace tree - so keep them.
        Langfuse(
            should_export_span=lambda span: is_default_export_span(span)
            or (
                span.instrumentation_scope is not None
                and span.instrumentation_scope.name.startswith("logfire")
            )
        )
        _langfuse_client = get_client()

        # Blocking call, so only on the first (cold) invocation
        try:
            _langfuse_client.auth_check()
            logger.info("✅ Observability: LangFuse authentication check passed")
        except Exception as auth_error:
            logger.warning(f"⚠️  Observability: Auth check failed but continuing: {auth_error}")

        logger.info("🎯 Observability: Setup complete - traces will be sent to LangFuse")
    except ImportError as e:
        logger.error(f"❌ Observability: Missing required package: {e}")
        _langfuse_client = None
    except Exception as e:
        logger.error(f"❌ Observability: Setup failed: {e}")
        _langfuse_client = None

    return _langfuse_client


@contextmanager
def observe():
    """
    Context manager for observability with LangFuse.

    Sets up LangFuse observability (once per container) if environment variables
    are configured, and flushes this invocation's traces on exit.

    Usage:
        from observability import observe

        with observe():
            # Your code that uses OpenAI Agents SDK
            pass
    """
    if not os.getenv("LANGFUSE_SECRET_KEY"):
        logger.info("🔍 Observability: LangFuse not configured, skipping setup")
        yield None
        return

    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("⚠️  Observability: OPENAI_API_KEY not set, traces may not export")

    langfuse_client = _setup()
    try:
        yield langfuse_client
    finally:
        if langfuse_client:
            try:
                # flush() blocks until queued spans are exported, so no sleep is
                # needed (the old fixed 10-15s sleep added ~30s to every job)
                langfuse_client.flush()
                logger.info("✅ Observability: Traces flushed")
            except Exception as e:
                logger.error(f"❌ Observability: Failed to flush traces: {e}")
