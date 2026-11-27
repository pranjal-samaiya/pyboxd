"""Request schema for ``POST /run``.

Models are strict and reject unknown fields, so typos like ``"test"`` instead of
``"tests"`` fail loudly with ``invalid_json`` instead of being ignored.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LimitsIn(_Strict):
    wall_time_s: int = 0
    memory_kb: int = 0
    max_processes: int = 0


class PhaseIn(_Strict):
    flags: list[str] = []
    limits: LimitsIn | None = None


class TestCaseIn(_Strict):
    stdin: str = ""
    expected_stdout: str = ""


class RunRequestIn(_Strict):
    language: str = ""
    source: str = ""
    source_filename: str = ""
    artifact_filename: str = ""
    build: PhaseIn | None = None
    run: PhaseIn | None = None
    tests: list[TestCaseIn] = []
