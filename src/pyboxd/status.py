"""Verdict vocabulary and aggregation rules."""

from __future__ import annotations

from collections.abc import Iterable

ACCEPTED = "accepted"
BUILD_FAILED = "build_failed"
WRONG_OUTPUT = "wrong_output"
OUTPUT_WHITESPACE_MISMATCH = "output_whitespace_mismatch"
TIME_EXCEEDED = "time_exceeded"
MEMORY_EXCEEDED = "memory_exceeded"
RUNTIME_ERROR = "runtime_error"
NOT_EXECUTED = "not_executed"
INTERNAL_ERROR = "internal_error"

# Build-phase statuses
BUILD_OK = "ok"
BUILD_FAILED_PHASE = "failed"


def top_level_status(build_status: str | None, test_statuses: Iterable[str]) -> str:
    """Accepted only if the build succeeded (or there was none) and every test passed.

    Otherwise: ``build_failed`` for a failed build, else the first non-accepted test status.
    """
    if build_status is not None and build_status != BUILD_OK:
        return BUILD_FAILED
    for status in test_statuses:
        if status != ACCEPTED:
            return status
    return ACCEPTED


def compare_output(actual: str, expected: str) -> str:
    """Exact match -> accepted; equal after trimming leading/trailing whitespace ->
    output_whitespace_mismatch; anything else -> wrong_output.

    Internal whitespace differences are deliberately not normalised.
    """
    if actual == expected:
        return ACCEPTED
    if actual.strip() == expected.strip():
        return OUTPUT_WHITESPACE_MISMATCH
    return WRONG_OUTPUT
