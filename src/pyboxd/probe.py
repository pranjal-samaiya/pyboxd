"""Readiness probes: is nsjail present, and does every toolchain respond?"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from . import nsjail
from .config import Language

PROBE_TIMEOUT_S = 5
_VERSION_ARGS = {"go": "version", "kotlinc": "-version", "iverilog": "-V", "java": "-version"}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    version: str = ""
    error: str = ""


async def probe_nsjail() -> ProbeResult:
    path = nsjail.nsjail_path()
    if not Path(path).exists():
        return ProbeResult(ok=False, error=f"{path} not found")
    if not os.access(path, os.X_OK):
        return ProbeResult(ok=False, error=f"{path} is not executable")
    # nsjail has no --version flag; printing the help banner proves it runs.
    result = await _version(path, "--help")
    if not result.ok:
        return result
    return ProbeResult(ok=True, version=os.environ.get("NSJAIL_VERSION", "unknown"))


async def probe_language(lang: Language) -> ProbeResult:
    """Run the compiler (or interpreter) with its version flag."""
    cmd = lang.build.cmd if lang.build else lang.run.cmd
    if "{{" in cmd:
        return ProbeResult(ok=False, error=f"cannot probe templated command {cmd!r}")
    arg = lang.version_arg or _VERSION_ARGS.get(Path(cmd).name, "--version")
    return await _version(cmd, arg)


async def _version(cmd: str, arg: str) -> ProbeResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            arg,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return ProbeResult(ok=False, error=str(exc))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ProbeResult(ok=False, error=f"{cmd} {arg} timed out")
    text = out.decode("utf-8", errors="replace")
    if proc.returncode != 0 and not text.strip():
        return ProbeResult(ok=False, error=f"{cmd} {arg} exited with {proc.returncode}")
    for line in text.splitlines():
        line = line.strip()
        if line and "warning:" not in line.lower() and not line.startswith("Picked up"):
            return ProbeResult(ok=True, version=line[:200])
    return ProbeResult(ok=True)


async def probe_all(languages: dict[str, Language]) -> tuple[ProbeResult, dict[str, ProbeResult]]:
    ids = sorted(languages)
    results = await asyncio.gather(probe_nsjail(), *(probe_language(languages[i]) for i in ids))
    return results[0], dict(zip(ids, results[1:], strict=True))
