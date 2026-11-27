from __future__ import annotations

import pytest

from pyboxd.validate import ValidationError, validate_filename, validate_flags


@pytest.mark.parametrize(
    "name",
    ["solution.py", "Main.java", "a", "my_file-1.cpp", "x" * 128, "résumé.py"],
)
def test_valid_filenames(name: str) -> None:
    validate_filename(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        ".hidden",
        "../etc/passwd",
        "a/b",
        "a\\b",
        "/abs",
        "nul\x00byte",
        "new\nline",
        "tab\tchar",
        "del\x7f",
        "x" * 129,
        "é" * 65,  # 130 bytes in UTF-8
        "usr",
        "tmp",
        "proc",
    ],
)
def test_invalid_filenames(name: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_filename(name)
    assert exc.value.code == "invalid_filename"


def test_flags_empty_is_ok() -> None:
    validate_flags([], [])
    validate_flags([], ["-O2"])


def test_flags_exact_and_glob() -> None:
    validate_flags(["-O2", "-std=c++17", "-std=c11"], ["-O2", "-std=*"])


@pytest.mark.parametrize(
    "flags",
    [
        ["-o/etc/passwd"],
        ["-fplugin=/tmp/evil.so"],
        ["-O2", "-wrapper", "/bin/sh"],
        ["-O2 -o x"],
        ["-std"],  # prefix "-std=" not matched
    ],
)
def test_flags_rejected(flags: list[str]) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_flags(flags, ["-O2", "-std=*"])
    assert exc.value.code == "disallowed_flag"


def test_flags_rejected_when_no_allowlist() -> None:
    with pytest.raises(ValidationError, match="no flags allowed"):
        validate_flags(["-O2"], [])


def test_flags_error_lists_all_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_flags(["-a", "-O2", "-b"], ["-O2"])
    assert "-a" in exc.value.message and "-b" in exc.value.message
