from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from pyboxd import nsjail
from pyboxd.config import Language, PhaseConfig, ResourceLimits
from pyboxd.runner import RunRequest, _strip_lines, expand_args, resolve_filenames
from pyboxd.sandbox import (
    TRUNCATION_MARKER,
    JailDir,
    cap_output,
    safe_join,
    sweep_orphaned_dirs,
    write_source,
)

# ---------------------------------------------------------------- templates


def test_expand_args() -> None:
    args = ("{{flags}}", "-o", "{{artifact}}", "{{source}}", "-DNAME={{class}}")
    out = expand_args(args, "/s.cpp", "/out/Main.class", ["-O2", "-Wall"])
    assert out == ["-O2", "-Wall", "-o", "/out/Main.class", "/s.cpp", "-DNAME=Main"]


def test_expand_args_no_flags() -> None:
    assert expand_args(("{{flags}}", "{{source}}"), "/a", "/b", []) == ["/a"]


def _java() -> Language:
    return Language(
        id="java",
        name="Java",
        run=PhaseConfig(cmd="java"),
        build=PhaseConfig(cmd="javac"),
        source_filename="Solution.java",
        source_filename_strategy="from_request",
        artifact="Solution.class",
        artifact_filename_strategy="from_request",
    )


def test_resolve_filenames_from_request() -> None:
    req = RunRequest(language="java", source="", tests=[], source_filename="Main.java")
    assert resolve_filenames(_java(), req) == ("Main.java", "Main.class")


def test_resolve_filenames_defaults() -> None:
    req = RunRequest(language="java", source="", tests=[])
    assert resolve_filenames(_java(), req) == ("Solution.java", "Solution.class")


def test_resolve_filenames_ignores_request_without_strategy() -> None:
    lang = Language(id="py3", name="Py", run=PhaseConfig(cmd="python3"), source_filename="s.py")
    req = RunRequest(language="py3", source="", tests=[], source_filename="evil.py")
    assert resolve_filenames(lang, req) == ("s.py", "solution")


def test_strip_lines() -> None:
    text = "1\nVCD info\n$finish called at 10\n2\n"
    assert _strip_lines(text, ("$finish called at",)) == "1\nVCD info\n2\n"
    assert _strip_lines(text, ()) == text


# ---------------------------------------------------------------- sandbox


def test_jail_dir_unique_and_cleaned(tmp_path: Path) -> None:
    with JailDir(tmp_path) as a, JailDir(tmp_path) as b:
        assert a != b
        assert a.name.startswith("pyboxd-") and a.is_dir()
        assert (a.stat().st_mode & 0o777) == 0o777
        (a / "f").write_text("x")
    assert not a.exists() and not b.exists()


def test_safe_join(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a.py") == tmp_path.resolve() / "a.py"
    # only the basename is ever used
    assert safe_join(tmp_path, "../../etc/passwd") == tmp_path.resolve() / "passwd"
    for bad in ("..", ".", ""):
        with pytest.raises(ValueError):
            safe_join(tmp_path, bad)


def test_write_source_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "solution.py"
    link.symlink_to(target)
    with pytest.raises(OSError):
        write_source(link, "pwned")
    assert target.read_text() == "keep"


def test_cap_output() -> None:
    assert cap_output(b"hello", 10, False) == "hello"
    assert cap_output(b"hello world", 5, True) == "hello" + TRUNCATION_MARKER
    assert cap_output(b"\xff", 10, False) == "\ufffd"


def test_sweep_removes_only_old_pyboxd_dirs(tmp_path: Path) -> None:
    old = tmp_path / "pyboxd-old"
    new = tmp_path / "pyboxd-new"
    other = tmp_path / "unrelated-old"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert sweep_orphaned_dirs(tmp_path, max_age_s=600) == 1
    assert not old.exists() and new.exists() and other.exists()


# ---------------------------------------------------------------- nsjail args


def _pairs(args: list[str]) -> list[tuple[str, str]]:
    return list(zip(args, args[1:], strict=False))


def test_run_args_isolation(tmp_path: Path) -> None:
    lim = ResourceLimits(3, 524288, 64)
    args = nsjail.run_args(tmp_path, lim, "./solution", ["x"], log_fd=7)
    pairs = _pairs(args)
    assert ("--chroot", str(tmp_path)) in pairs
    assert "--rw" not in args  # run phase root is read-only
    assert ("--time_limit", "3") in pairs
    assert ("--rlimit_as", "512") in pairs
    assert ("--rlimit_nproc", "64") in pairs
    assert ("--log_fd", "7") in pairs
    assert ("--user", "65534:65534:1") in pairs
    assert ("--group", "65534:65534:1") in pairs
    assert ("--seccomp_string", nsjail.SECCOMP_POLICY) in pairs
    assert args[args.index("--") + 1 :] == ["./solution", "x"]


def test_build_args_isolation(tmp_path: Path) -> None:
    args = nsjail.build_args(tmp_path, ResourceLimits(10, 1048576, 100), "/usr/bin/gcc", ["-v"])
    pairs = _pairs(args)
    assert ("--chroot", str(tmp_path)) in pairs  # never the host root
    assert ("--chroot", "/") not in pairs
    assert "--rw" in args
    assert ("--tmpfsmount", "/tmp") in pairs
    assert ("--bindmount", "/tmp:/tmp") not in pairs


def test_rlimit_as_override_and_cgroup(tmp_path: Path) -> None:
    args = nsjail.run_args(
        tmp_path, ResourceLimits(5, 1024, 8), "java", [], rlimit_as_mb=4096, cgroup_name="cg"
    )
    pairs = _pairs(args)
    assert ("--rlimit_as", "4096") in pairs
    assert ("--cgroupv2_mount", "/sys/fs/cgroup/cg") in pairs
    assert "--use_cgroupv2" in args


def test_extra_bindmounts(tmp_path: Path) -> None:
    args = nsjail.run_args(
        tmp_path, ResourceLimits(1, 1024, 1), "x", [], extra_bindmounts_ro=("/opt/a:/opt/a",)
    )
    assert ("--bindmount_ro", "/opt/a:/opt/a") in _pairs(args)


def test_seccomp_blocks_dangerous_syscalls() -> None:
    for syscall in ("ptrace", "mount", "unshare", "setns", "bpf", "kexec_load"):
        assert syscall in nsjail.SECCOMP_POLICY
