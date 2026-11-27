"""Execution engine: build once, run every test case, map outcomes to verdicts.

Every child process is started with ``asyncio.create_subprocess_exec`` so the
event loop never blocks while user code runs. Concurrency limits are the
caller's job (see ``app.py``); this module only runs one request at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import secrets
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import nsjail
from . import status as st
from .config import (
    STRATEGY_FROM_REQUEST,
    TEMPLATE_ARTIFACT,
    TEMPLATE_CLASS,
    TEMPLATE_FLAGS,
    TEMPLATE_SOURCE,
    Language,
    ResourceLimits,
)
from .sandbox import DEFAULT_MAX_OUTPUT_BYTES, JailDir, cap_output, safe_join, write_source

log = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
KILL_GRACE_S = 2  # extra time so nsjail's own --time_limit fires first

# Substrings in stderr that mean "the program ran out of memory" across runtimes.
MEMORY_ERROR_MARKERS = (
    "MemoryError",
    "std::bad_alloc",
    "java.lang.OutOfMemoryError",
    "JavaScript heap out of memory",
    "memory allocation of",
    "Cannot allocate memory",
)


class SandboxMode(StrEnum):
    NSJAIL = "nsjail"
    UNSANDBOXED = "unsandboxed"  # dev/CI only: rlimits but no isolation
    UNAVAILABLE = "unavailable"


def detect_sandbox_mode() -> SandboxMode:
    if nsjail.nsjail_available():
        return SandboxMode.NSJAIL
    if os.environ.get("PYBOXD_ALLOW_UNSANDBOXED") == "1":
        return SandboxMode.UNSANDBOXED
    return SandboxMode.UNAVAILABLE


class SandboxError(RuntimeError):
    """The sandbox itself failed (not the user's program)."""


# --------------------------------------------------------------------------- types


@dataclass
class PhaseOptions:
    flags: list[str] = field(default_factory=list)
    limits: ResourceLimits | None = None


@dataclass
class TestCase:
    __test__ = False  # not a pytest test class
    stdin: str = ""
    expected_stdout: str = ""


@dataclass
class RunRequest:
    language: str
    source: str
    tests: list[TestCase]
    source_filename: str = ""
    artifact_filename: str = ""
    build: PhaseOptions = field(default_factory=PhaseOptions)
    run: PhaseOptions = field(default_factory=PhaseOptions)


@dataclass
class BuildResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class
    status: str
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    memory_peak_kb: int = 0


@dataclass
class RunResult:
    status: str
    build: BuildResult | None
    tests: list[TestResult]


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    memory_exceeded: bool = False
    memory_peak_kb: int = 0
    sandbox_log: str = ""


# --------------------------------------------------------------------------- helpers


def expand_args(args: tuple[str, ...], source: str, artifact: str, flags: list[str]) -> list[str]:
    """Expand ``{{source}}``, ``{{artifact}}``, ``{{class}}`` and ``{{flags}}``.

    ``{{flags}}`` must be a whole argument and expands to zero or more args.
    """
    cls = Path(artifact).stem
    out: list[str] = []
    for arg in args:
        if arg == TEMPLATE_FLAGS:
            out.extend(flags)
            continue
        out.append(
            arg.replace(TEMPLATE_SOURCE, source)
            .replace(TEMPLATE_ARTIFACT, artifact)
            .replace(TEMPLATE_CLASS, cls)
        )
    return out


def resolve_filenames(lang: Language, req: RunRequest) -> tuple[str, str]:
    """Pick the source and artifact filenames for this request."""
    source = lang.source_filename
    if req.source_filename and lang.source_filename_strategy == STRATEGY_FROM_REQUEST:
        source = req.source_filename
    source = source or "solution"

    artifact = lang.artifact
    if lang.artifact_filename_strategy == STRATEGY_FROM_REQUEST:
        artifact = req.artifact_filename or Path(source).stem + Path(lang.artifact).suffix
    return source, artifact or "solution"


_cgroups_failed = False  # set once nsjail proved unable to use cgroups on this host


def _cgroups_enabled() -> bool:
    if _cgroups_failed:
        return False
    if os.environ.get("PYBOXD_CGROUPS", "auto").lower() in ("0", "off", "false", "no"):
        return False
    controllers = CGROUP_ROOT / "cgroup.controllers"
    try:
        return "memory" in controllers.read_text().split()
    except OSError:
        return False


class _Cgroup:
    """Parent cgroup (v2) used to read the peak memory and OOM kills of one command."""

    def __init__(self) -> None:
        self.name = f"pyboxd-{os.getpid()}-{secrets.token_hex(6)}"
        self.path = CGROUP_ROOT / self.name

    def create(self) -> bool:
        try:
            self.path.mkdir(mode=0o755)
        except OSError as exc:
            log.warning("cgroup create failed; memory stats disabled", extra={"err": str(exc)})
            return False
        with contextlib.suppress(OSError):
            (self.path / "cgroup.subtree_control").write_text("+memory")
        return True

    def _read(self, name: str) -> str:
        try:
            return (self.path / name).read_text()
        except OSError:
            return ""

    def peak_kb(self) -> int:
        raw = self._read("memory.peak").strip()
        return int(raw) // 1024 if raw.isdigit() else 0

    def oom_killed(self) -> bool:
        for line in self._read("memory.events").splitlines():
            key, _, value = line.partition(" ")
            if key in ("oom_kill", "oom_group_kill") and value.strip() not in ("", "0"):
                return True
        return False

    def remove(self) -> None:
        with contextlib.suppress(OSError):
            for child in self.path.iterdir():
                if child.is_dir():
                    with contextlib.suppress(OSError):
                        child.rmdir()
        with contextlib.suppress(OSError):
            self.path.rmdir()


def _looks_like_oom(log_text: str) -> bool:
    low = log_text.lower()
    return any(marker in low for marker in ("memory limit exceeded", "out of memory", "oom kill"))


async def _read_capped(stream: asyncio.StreamReader | None, sink: bytearray, limit: int) -> bool:
    """Read a stream to EOF, keeping at most ``limit`` bytes. Returns True if truncated."""
    if stream is None:
        return False
    truncated = False
    while chunk := await stream.read(65536):
        room = limit - len(sink)
        if room > 0:
            sink.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            truncated = True  # keep draining so the child never blocks on a full pipe
    return truncated


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    if proc.stdin is None:
        return
    try:
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # program exited without reading all its input
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()


def _rlimit_preexec(limits: ResourceLimits, rlimit_as_mb: int) -> Callable[[], None]:
    """rlimits for unsandboxed dev mode (no namespaces, so no process cap)."""
    mem_bytes = (rlimit_as_mb * 1024 * 1024) if rlimit_as_mb else limits.memory_kb * 1024

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.wall_time_s, limits.wall_time_s + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


# --------------------------------------------------------------------------- command


async def run_command(**kwargs: Any) -> CommandResult:
    """Run one command (build or test) inside the sandbox and collect its outcome.

    cgroup v2 memory accounting is best-effort: if nsjail cannot start with it on
    this host, it is switched off (rlimits still apply) and the command retried.
    """
    global _cgroups_failed
    use_cgroups = _cgroups_enabled()
    try:
        return await _run_command_once(use_cgroups=use_cgroups, **kwargs)
    except SandboxError:
        if not use_cgroups:
            raise
        _cgroups_failed = True
        log.warning("nsjail failed with cgroups enabled; disabling cgroup memory accounting")
        return await _run_command_once(use_cgroups=False, **kwargs)


async def _run_command_once(
    *,
    use_cgroups: bool,
    mode: SandboxMode,
    phase: str,
    lang: Language,
    jail: Path,
    limits: ResourceLimits,
    cmd: str,
    args: list[str],
    stdin: bytes | None,
    max_output: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> CommandResult:
    cgroup: _Cgroup | None = None
    log_r = log_w = -1
    kwargs: dict[str, object] = {}

    if mode is SandboxMode.NSJAIL:
        if use_cgroups:
            cgroup = _Cgroup()
            if not cgroup.create():
                cgroup = None
        log_r, log_w = os.pipe()
        builder = nsjail.build_args if phase == "build" else nsjail.run_args
        extra: dict[str, object] = {}
        if phase == "run":
            extra["extra_bindmounts_ro"] = lang.extra_bindmounts_ro
        argv = [
            nsjail.nsjail_path(),
            *builder(
                jail,
                limits,
                cmd,
                args,
                rlimit_as_mb=lang.rlimit_as_mb,
                log_fd=log_w,
                cgroup_name=cgroup.name if cgroup else None,
                **extra,
            ),
        ]
        if cgroup is not None:
            argv[1:1] = ["--cgroup_mem_max", str(limits.memory_kb * 1024)]
        kwargs["pass_fds"] = (log_w,)
    elif mode is SandboxMode.UNSANDBOXED:
        argv = [cmd, *args]
        kwargs["cwd"] = str(jail)
        kwargs["env"] = {
            "PATH": nsjail.JAIL_PATH_ENV.removeprefix("PATH="),
            "HOME": str(jail),
            "TMPDIR": str(jail),
        }
        kwargs["preexec_fn"] = _rlimit_preexec(limits, lang.rlimit_as_mb)
    else:
        raise SandboxError("no sandbox available (nsjail not found)")

    out_buf, err_buf, log_buf = bytearray(), bytearray(), bytearray()
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            **kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        for fd in (log_r, log_w):
            if fd >= 0:
                os.close(fd)
        if cgroup:
            cgroup.remove()
        if mode is SandboxMode.UNSANDBOXED:
            # e.g. missing interpreter or non-executable artifact
            return CommandResult(stderr=str(exc), exit_code=127, elapsed_ms=0)
        raise SandboxError(f"failed to start sandbox: {exc}") from exc

    log_reader: asyncio.StreamReader | None = None
    log_transport: asyncio.BaseTransport | None = None
    if log_w >= 0:
        os.close(log_w)  # the child holds its own copy
        loop = asyncio.get_running_loop()
        log_reader = asyncio.StreamReader()
        log_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(log_reader),
            os.fdopen(log_r, "rb", 0),
        )

    async def collect() -> tuple[bool, bool]:
        results = await asyncio.gather(
            _feed_stdin(proc, stdin or b""),
            _read_capped(proc.stdout, out_buf, max_output),
            _read_capped(proc.stderr, err_buf, max_output),
            _read_capped(log_reader, log_buf, 256 * 1024),
            proc.wait(),
        )
        return results[1], results[2]

    timed_out = False
    out_trunc = err_trunc = False
    try:
        out_trunc, err_trunc = await asyncio.wait_for(
            collect(), timeout=limits.wall_time_s + KILL_GRACE_S
        )
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
    finally:
        if log_transport is not None:
            log_transport.close()
        # Unsandboxed: kill stragglers left in the process group.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    if elapsed_ms >= limits.wall_time_s * 1000:
        timed_out = True

    log_text = log_buf.decode("utf-8", errors="replace")
    exit_code = proc.returncode
    stderr_text = cap_output(bytes(err_buf), max_output, err_trunc)

    if mode is SandboxMode.NSJAIL and not timed_out and "Executing '" not in log_text:
        if cgroup:
            cgroup.remove()
        # nsjail failed before it could start the program: an infrastructure error.
        errors = [ln for ln in log_text.splitlines() if ln.startswith(("[E]", "[F]"))]
        raise SandboxError("sandbox failed to start: " + ("; ".join(errors) or log_text[-500:]))

    killed_by_cpu = "SIGXCPU" in log_text or exit_code in (-signal.SIGXCPU, 128 + signal.SIGXCPU)
    memory_exceeded = False
    peak_kb = 0
    if cgroup is not None:
        peak_kb = cgroup.peak_kb()
        memory_exceeded = cgroup.oom_killed()
        cgroup.remove()
    if not memory_exceeded and exit_code not in (0, None) and not timed_out:
        memory_exceeded = _looks_like_oom(log_text) or any(
            marker in stderr_text for marker in MEMORY_ERROR_MARKERS
        )

    return CommandResult(
        stdout=cap_output(bytes(out_buf), max_output, out_trunc),
        stderr=stderr_text,
        elapsed_ms=elapsed_ms,
        exit_code=exit_code,
        timed_out=timed_out or killed_by_cpu,
        memory_exceeded=memory_exceeded,
        memory_peak_kb=peak_kb,
        sandbox_log=log_text,
    )


# --------------------------------------------------------------------------- request


def _jail_path(mode: SandboxMode, jail: Path, name: str) -> str:
    """Path of a jail file as the sandboxed process sees it."""
    return f"/{name}" if mode is SandboxMode.NSJAIL else str(jail / name)


def _strip_lines(text: str, needles: tuple[str, ...]) -> str:
    if not needles:
        return text
    return "\n".join(ln for ln in text.split("\n") if not any(n in ln for n in needles))


async def run_sandbox(lang: Language, req: RunRequest, mode: SandboxMode) -> RunResult:
    """Full lifecycle of one ``POST /run``: jail dir, build, run each test, verdict."""
    if mode is SandboxMode.UNAVAILABLE:
        raise SandboxError("no sandbox available (nsjail not found)")

    source_name, artifact_name = resolve_filenames(lang, req)
    with JailDir() as jail:
        write_source(safe_join(jail, source_name), req.source)
        source_path = _jail_path(mode, jail, source_name)
        artifact_path = _jail_path(mode, jail, artifact_name)

        build_res: BuildResult | None = None
        if lang.build is not None:
            limits = lang.build_limits().merge_with_cap(req.build.limits)
            res = await run_command(
                mode=mode,
                phase="build",
                lang=lang,
                jail=jail,
                limits=limits,
                cmd=lang.build.cmd,
                args=expand_args(lang.build.args, source_path, artifact_path, req.build.flags),
                stdin=None,
            )
            ok = res.exit_code == 0 and not res.timed_out
            build_res = BuildResult(
                status=st.BUILD_OK if ok else st.BUILD_FAILED_PHASE,
                stdout=res.stdout,
                stderr=res.stderr,
                duration_ms=res.elapsed_ms,
            )
            if not ok:
                if res.timed_out:
                    build_res.stderr += "\n[build time limit exceeded]"
                return RunResult(
                    status=st.BUILD_FAILED,
                    build=build_res,
                    tests=[TestResult(status=st.NOT_EXECUTED) for _ in req.tests],
                )

        run_limits = lang.run_limits().merge_with_cap(req.run.limits)
        run_cmd = (
            lang.run.cmd.replace(TEMPLATE_ARTIFACT, artifact_name)
            .replace(TEMPLATE_SOURCE, source_name)
            .replace(TEMPLATE_CLASS, Path(artifact_name).stem)
        )
        run_argv = expand_args(lang.run.args, source_path, artifact_path, req.run.flags)

        results: list[TestResult] = []
        for tc in req.tests:
            res = await run_command(
                mode=mode,
                phase="run",
                lang=lang,
                jail=jail,
                limits=run_limits,
                cmd=run_cmd,
                args=run_argv,
                stdin=tc.stdin.encode("utf-8"),
            )
            stdout = _strip_lines(res.stdout, lang.strip_stdout_lines_containing)
            if res.timed_out:
                verdict = st.TIME_EXCEEDED
            elif res.memory_exceeded:
                verdict = st.MEMORY_EXCEEDED
            elif res.exit_code != 0:
                verdict = st.RUNTIME_ERROR
            else:
                verdict = st.compare_output(stdout, tc.expected_stdout)
            results.append(
                TestResult(
                    status=verdict,
                    stdout=stdout,
                    stderr=res.stderr,
                    duration_ms=res.elapsed_ms,
                    memory_peak_kb=res.memory_peak_kb,
                )
            )

        return RunResult(
            status=st.top_level_status(
                build_res.status if build_res else None, (r.status for r in results)
            ),
            build=build_res,
            tests=results,
        )
