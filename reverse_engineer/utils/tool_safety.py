"""Shared decorator so tool functions report failures to the agent instead of crashing the run."""

import functools


def tool_safe(func):
    """Wrap a Deep Agents tool function so any exception it raises becomes its return value.

    Subagents call these functions directly as tools; an uncaught exception
    here propagates up through the agent's tool-calling loop and can abort
    the entire run instead of letting the agent see what went wrong and
    decide how to proceed (retry with different arguments, skip, or report
    the failure upward). This decorator catches any exception and returns a
    structured error payload instead, so a single bad tool call degrades
    gracefully rather than stopping the pipeline.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            return {
                "ok": False,
                "tool": func.__name__,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }

    return wrapper
