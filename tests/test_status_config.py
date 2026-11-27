from __future__ import annotations

from pathlib import Path

import pytest

from pyboxd import status as st
from pyboxd.config import ConfigError, ResourceLimits, load, parse

# ---------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("actual", "expected", "verdict"),
    [
        ("42\n", "42\n", st.ACCEPTED),
        ("42", "42\n", st.OUTPUT_WHITESPACE_MISMATCH),
        ("  42  \n\n", "42\n", st.OUTPUT_WHITESPACE_MISMATCH),
        ("4 2\n", "4  2\n", st.WRONG_OUTPUT),  # internal whitespace is not normalised
        ("41\n", "42\n", st.WRONG_OUTPUT),
        ("", "", st.ACCEPTED),
    ],
)
def test_compare_output(actual: str, expected: str, verdict: str) -> None:
    assert st.compare_output(actual, expected) == verdict


def test_top_level_status() -> None:
    assert st.top_level_status(None, [st.ACCEPTED, st.ACCEPTED]) == st.ACCEPTED
    assert st.top_level_status("ok", [st.ACCEPTED]) == st.ACCEPTED
    assert st.top_level_status("failed", [st.NOT_EXECUTED]) == st.BUILD_FAILED
    assert (
        st.top_level_status(None, [st.ACCEPTED, st.TIME_EXCEEDED, st.WRONG_OUTPUT])
        == st.TIME_EXCEEDED
    )


# ---------------------------------------------------------------- limits


def test_merge_with_cap_allows_lower_values() -> None:
    cap = ResourceLimits(10, 1024, 64)
    assert cap.merge_with_cap(ResourceLimits(2, 0, 0)) == ResourceLimits(2, 1024, 64)
    assert cap.merge_with_cap(None) == cap


@pytest.mark.parametrize(
    "override",
    [ResourceLimits(11, 0, 0), ResourceLimits(0, 2048, 0), ResourceLimits(0, 0, 65)],
)
def test_merge_with_cap_rejects_higher_values(override: ResourceLimits) -> None:
    with pytest.raises(ValueError, match="exceeds maximum"):
        ResourceLimits(10, 1024, 64).merge_with_cap(override)


def test_merge_with_cap_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        ResourceLimits(10, 1024, 64).merge_with_cap(ResourceLimits(-1, 0, 0))


def test_with_defaults() -> None:
    got = ResourceLimits(5, 0, 0).with_defaults(ResourceLimits(10, 100, 7))
    assert got == ResourceLimits(5, 100, 7)


# ---------------------------------------------------------------- config


def test_repo_languages_yaml_loads(repo_config) -> None:  # type: ignore[no-untyped-def]
    assert {"py3", "c", "cpp", "java", "javascript", "bash", "verilog", "rust"} <= set(
        repo_config.languages
    )
    java = repo_config.languages["java"]
    assert java.source_filename_strategy == "from_request"
    assert java.rlimit_as_mb == 4096
    assert repo_config.max_concurrent >= 1


def _minimal(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {"languages": {"py3": {"name": "Python", "run": {"cmd": "python3"}}}}
    raw.update(overrides)
    return raw


def test_defaults_applied() -> None:
    cfg = parse(_minimal())
    assert cfg.max_body_bytes == 4 * 1024 * 1024
    assert cfg.max_source_bytes == 256 * 1024
    assert cfg.max_tests == 50
    assert cfg.queue_timeout_s == 30
    lang = cfg.languages["py3"]
    assert lang.source_filename == "solution"
    assert lang.run_limits() == ResourceLimits(10, 256 * 1024, 64)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "no languages"),
        ({"languages": {}}, "no languages"),
        ({"languages": {"x": {"run": {"cmd": "a"}}}}, "name"),
        ({"languages": {"x": {"name": "X"}}}, "run is required"),
        ({"languages": {"x": {"name": "X", "run": {"cmd": ""}}}}, "cmd"),
        ({"languages": {"x": {"id": "y", "name": "X", "run": {"cmd": "a"}}}}, "must match"),
        (
            {"languages": {"x": {"name": "X", "run": {"cmd": "a"}, "build": {"args": []}}}},
            "build.cmd",
        ),
        (
            {"languages": {"x": {"name": "X", "run": {"cmd": "a", "limits": {"memory_kb": "1"}}}}},
            "integer",
        ),
        (_minimal(max_tests=0), "max_tests"),
        (_minimal(max_source_bytes=-1), "max_source_bytes"),
    ],
)
def test_invalid_configs(raw: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(raw)


def test_load_missing_and_malformed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="failed to read"):
        load(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("languages: [unclosed")
    with pytest.raises(ConfigError, match="parse YAML"):
        load(bad)
