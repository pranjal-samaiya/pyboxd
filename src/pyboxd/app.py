"""FastAPI application: routes, auth, request logging and background tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError as PydanticValidationError

from . import __version__, auth, nsjail
from .config import Config, Language, ResourceLimits
from .models import PhaseIn, RunRequestIn
from .probe import ProbeResult, probe_all
from .runner import (
    PhaseOptions,
    RunRequest,
    RunResult,
    SandboxError,
    SandboxMode,
    TestCase,
    detect_sandbox_mode,
    run_sandbox,
)
from .sandbox import jail_base_dir, sweep_orphaned_dirs
from .validate import ValidationError, validate_filename, validate_flags

log = logging.getLogger("pyboxd")

SWEEP_INTERVAL_S = 600
SWEEP_MAX_AGE_S = 600
REPROBE_INTERVAL_S = 60


@dataclass
class Settings:
    config: Config
    auth_token: str
    version: str = __version__
    commit: str = "unknown"
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    sandbox_mode: SandboxMode | None = None  # None = detect at startup
    background_tasks: bool = True


@dataclass
class Stats:
    in_flight_jobs: int = 0
    jobs_total: int = 0
    jobs_failed_internal: int = 0
    last_internal_error_at: datetime | None = None


@dataclass
class State:
    settings: Settings
    semaphore: asyncio.Semaphore
    mode: SandboxMode
    stats: Stats = field(default_factory=Stats)
    nsjail_probe: ProbeResult = field(default_factory=lambda: ProbeResult(ok=False))
    lang_probes: dict[str, ProbeResult] = field(default_factory=dict)


class AuthError(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


# --------------------------------------------------------------------------- validation


class BadRequest(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


def _limits(phase: PhaseIn | None) -> ResourceLimits | None:
    if phase is None or phase.limits is None:
        return None
    lim = phase.limits
    return ResourceLimits(lim.wall_time_s, lim.memory_kb, lim.max_processes)


def validate_request(cfg: Config, req: RunRequestIn) -> tuple[Language, RunRequest]:
    """Apply every API-level check and return the runner request. Raises ``BadRequest``."""
    if len(req.source.encode("utf-8")) > cfg.max_source_bytes:
        raise BadRequest(
            "source_too_large", f"source exceeds maximum size of {cfg.max_source_bytes} bytes"
        )
    lang = cfg.languages.get(req.language)
    if lang is None:
        raise BadRequest("unknown_language", f"language {req.language!r} is not configured")

    try:
        for name in (req.source_filename, req.artifact_filename):
            if name:
                validate_filename(name)
        build_flags = req.build.flags if req.build else []
        run_flags = req.run.flags if req.run else []
        if build_flags:
            if lang.build is None:
                raise ValidationError(
                    "disallowed_flag", f"language {lang.id!r} does not support build flags"
                )
            validate_flags(build_flags, lang.build.flag_allowlist)
        validate_flags(run_flags, lang.run.flag_allowlist)
    except ValidationError as exc:
        raise BadRequest(exc.code, exc.message) from exc

    build_limits, run_limits = _limits(req.build), _limits(req.run)
    try:
        if build_limits is not None:
            if lang.build is None:
                raise ValueError(f"language {lang.id!r} does not support build limits")
            lang.build_limits().merge_with_cap(build_limits)
        if run_limits is not None:
            lang.run_limits().merge_with_cap(run_limits)
    except ValueError as exc:
        raise BadRequest("invalid_limits", str(exc)) from exc

    if not 1 <= len(req.tests) <= cfg.max_tests:
        raise BadRequest("invalid_test_count", f"test count must be between 1 and {cfg.max_tests}")

    return lang, RunRequest(
        language=req.language,
        source=req.source,
        source_filename=req.source_filename,
        artifact_filename=req.artifact_filename,
        build=PhaseOptions(flags=list(build_flags), limits=build_limits),
        run=PhaseOptions(flags=list(run_flags), limits=run_limits),
        tests=[TestCase(t.stdin, t.expected_stdout) for t in req.tests],
    )


async def read_body_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise BadRequest("body_too_large", f"request body exceeds {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise BadRequest("body_too_large", f"request body exceeds {limit} bytes")
    return bytes(body)


def result_to_json(result: RunResult) -> dict[str, Any]:
    out: dict[str, Any] = {"status": result.status}
    if result.build is not None:
        b = result.build
        out["build"] = {
            "status": b.status,
            "stdout": b.stdout,
            "stderr": b.stderr,
            "duration_ms": b.duration_ms,
        }
    out["tests"] = [
        {
            "status": t.status,
            "stdout": t.stdout,
            "stderr": t.stderr,
            "duration_ms": t.duration_ms,
            "memory_peak_kb": t.memory_peak_kb,
        }
        for t in result.tests
    ]
    return out


# --------------------------------------------------------------------------- app


def create_app(settings: Settings) -> FastAPI:
    auth.check_token_config(settings.auth_token)
    cfg = settings.config

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state: State = app.state.pyboxd
        if state.mode is SandboxMode.UNAVAILABLE:
            log.error("nsjail not found: /run will refuse jobs (set NSJAIL_PATH)")
        elif state.mode is SandboxMode.UNSANDBOXED:
            log.warning("UNSANDBOXED MODE: user code runs without isolation. Dev use only.")
        tasks: list[asyncio.Task[None]] = []
        if settings.background_tasks:
            await asyncio.to_thread(sweep_orphaned_dirs, None, SWEEP_MAX_AGE_S)
            await refresh_probes(state)
            tasks = [
                asyncio.create_task(_every(SWEEP_INTERVAL_S, _sweep)),
                asyncio.create_task(_every(REPROBE_INTERVAL_S, lambda: refresh_probes(state))),
            ]
        log.info(
            "pyboxd ready",
            extra={
                "version": settings.version,
                "mode": state.mode.value,
                "max_concurrent": cfg.max_concurrent,
                "languages": sorted(cfg.languages),
            },
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="PyBoxD",
        version=settings.version,
        description="Secure sandboxed code execution service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.pyboxd = State(
        settings=settings,
        semaphore=asyncio.Semaphore(cfg.max_concurrent),
        mode=settings.sandbox_mode or detect_sandbox_mode(),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.middleware("http")
    async def request_log(request: Request, call_next: Any) -> Response:
        request_id = secrets.token_hex(8)
        request.state.request_id = request_id
        start = time.monotonic()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            log.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                    "language": getattr(request.state, "language", ""),
                    "job_status": getattr(request.state, "job_status", ""),
                },
            )

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return exc.response

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return error(500, "internal_error", "internal server error")

    async def require_auth(request: Request) -> None:
        denied = auth.verify(request, settings.auth_token)
        if denied is not None:
            raise AuthError(denied)

    # ---- public
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/info", status_code=302)

    # ---- protected
    protected = APIRouter(dependencies=[Depends(require_auth)])

    @protected.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        state: State = request.app.state.pyboxd
        nsj = state.nsjail_probe
        langs: dict[str, dict[str, Any]] = {}
        all_ok = nsj.ok
        for lang_id in sorted(cfg.languages):
            probe = state.lang_probes.get(lang_id, ProbeResult(ok=False, error="not probed yet"))
            all_ok = all_ok and probe.ok
            entry: dict[str, Any] = {"ok": probe.ok}
            if probe.version:
                entry["version"] = probe.version
            if probe.error:
                entry["error"] = probe.error
            langs[lang_id] = entry
        nsj_entry: dict[str, Any] = {"ok": nsj.ok, "version": nsj.version}
        if nsj.error:
            nsj_entry["error"] = nsj.error
        body = {
            "status": "ok" if all_ok else "degraded",
            "sandbox_mode": state.mode.value,
            "nsjail": nsj_entry,
            "languages": langs,
        }
        return JSONResponse(status_code=200 if all_ok else 503, content=body)

    @protected.get("/info")
    async def info(request: Request) -> dict[str, Any]:
        state: State = request.app.state.pyboxd
        try:
            disk_free = shutil.disk_usage(jail_base_dir()).free
        except OSError:
            disk_free = 0
        languages = []
        for lang_id in sorted(cfg.languages):
            lang = cfg.languages[lang_id]
            lim = lang.run_limits()
            languages.append(
                {
                    "id": lang_id,
                    "name": lang.name,
                    "version": state.lang_probes.get(lang_id, ProbeResult(ok=False)).version,
                    "default_run_limits": {
                        "wall_time_s": lim.wall_time_s,
                        "memory_kb": lim.memory_kb,
                        "max_processes": lim.max_processes,
                    },
                }
            )
        s = state.stats
        stats: dict[str, Any] = {
            "in_flight_jobs": s.in_flight_jobs,
            "jobs_total": s.jobs_total,
            "jobs_failed_internal": s.jobs_failed_internal,
            "disk_free_bytes_jail_dir": disk_free,
        }
        if s.last_internal_error_at is not None:
            stats["last_internal_error_at"] = s.last_internal_error_at.isoformat()
        return {
            "build_info": {
                "version": settings.version,
                "commit": settings.commit,
                "python_version": platform.python_version(),
            },
            "sandbox_mode": state.mode.value,
            "nsjail": {"path": nsjail.nsjail_path(), "version": state.nsjail_probe.version},
            "languages": languages,
            "limits": {
                "max_source_bytes": cfg.max_source_bytes,
                "max_tests": cfg.max_tests,
                "max_concurrent_jobs": cfg.max_concurrent,
            },
            "stats": stats,
        }

    @protected.post("/run")
    async def run(request: Request) -> JSONResponse:
        state: State = request.app.state.pyboxd
        try:
            body = await read_body_capped(request, cfg.max_body_bytes)
            try:
                parsed = RunRequestIn.model_validate_json(body)
            except PydanticValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(p) for p in first["loc"]) or "body"
                raise BadRequest("invalid_json", f"{where}: {first['msg']}") from exc
            request.state.language = parsed.language
            lang, run_req = validate_request(cfg, parsed)
        except BadRequest as exc:
            request.state.job_status = "rejected"
            return error(400, exc.code, exc.message)

        if state.mode is SandboxMode.UNAVAILABLE:
            return error(503, "sandbox_unavailable", "nsjail is not available on this server")

        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=cfg.queue_timeout_s)
        except TimeoutError:
            request.state.job_status = "queue_full"
            return error(
                429, "queue_full", f"server is busy, retry after {cfg.queue_timeout_s} seconds"
            )

        state.stats.in_flight_jobs += 1
        state.stats.jobs_total += 1
        try:
            result = await run_sandbox(lang, run_req, state.mode)
        except (SandboxError, OSError) as exc:
            state.stats.jobs_failed_internal += 1
            state.stats.last_internal_error_at = datetime.now(UTC)
            request.state.job_status = "internal_error"
            log.error("sandbox failure", extra={"err": str(exc), "language": lang.id})
            return error(500, "internal_error", str(exc))
        finally:
            state.stats.in_flight_jobs -= 1
            state.semaphore.release()

        request.state.job_status = result.status
        return JSONResponse(status_code=200, content=result_to_json(result))

    app.include_router(protected)
    return app


async def refresh_probes(state: State) -> None:
    state.nsjail_probe, state.lang_probes = await probe_all(state.settings.config.languages)


async def _sweep() -> None:
    await asyncio.to_thread(sweep_orphaned_dirs, None, SWEEP_MAX_AGE_S)


async def _every(interval_s: float, fn: Any) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await fn()
        except Exception:
            log.exception("background task failed")
