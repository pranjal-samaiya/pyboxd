"""YAML-driven language registry and global settings.

Adding a language never requires Python changes: add one block under
``languages:`` in ``languages.yaml`` and one install script in
``scripts/lang_install/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Template placeholders expanded at runtime.
TEMPLATE_SOURCE = "{{source}}"
TEMPLATE_ARTIFACT = "{{artifact}}"
TEMPLATE_CLASS = "{{class}}"
TEMPLATE_FLAGS = "{{flags}}"

STRATEGY_FROM_REQUEST = "from_request"


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class ResourceLimits:
    """Resource limits for one phase. ``0`` means "not set"."""

    wall_time_s: int = 0
    memory_kb: int = 0
    max_processes: int = 0

    def with_defaults(self, defaults: ResourceLimits) -> ResourceLimits:
        """Fill unset (<= 0) fields from ``defaults``."""
        return ResourceLimits(
            wall_time_s=self.wall_time_s if self.wall_time_s > 0 else defaults.wall_time_s,
            memory_kb=self.memory_kb if self.memory_kb > 0 else defaults.memory_kb,
            max_processes=self.max_processes if self.max_processes > 0 else defaults.max_processes,
        )

    def merge_with_cap(self, override: ResourceLimits | None) -> ResourceLimits:
        """Apply a per-request override, refusing anything above these limits.

        ``self`` acts as the ceiling. Unset override fields keep the ceiling value.
        Raises ``ValueError`` if the override asks for more than allowed.
        """
        if override is None:
            return self
        out = self
        for name in ("wall_time_s", "memory_kb", "max_processes"):
            requested = getattr(override, name)
            if requested < 0:
                raise ValueError(f"{name} must not be negative (got {requested})")
            if requested == 0:
                continue
            ceiling = getattr(self, name)
            if ceiling > 0 and requested > ceiling:
                raise ValueError(
                    f"requested {name} ({requested}) exceeds maximum allowed ({ceiling})"
                )
            out = replace(out, **{name: requested})
        return out


# Defaults used when a language leaves a limit unset.
DEFAULT_BUILD_LIMITS = ResourceLimits(wall_time_s=30, memory_kb=1024 * 1024, max_processes=100)
DEFAULT_RUN_LIMITS = ResourceLimits(wall_time_s=10, memory_kb=256 * 1024, max_processes=64)


@dataclass(frozen=True)
class PhaseConfig:
    cmd: str
    args: tuple[str, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    flag_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class Language:
    id: str
    name: str
    run: PhaseConfig
    build: PhaseConfig | None = None
    source_filename: str = "solution"
    source_filename_strategy: str = ""
    artifact: str = ""
    artifact_filename_strategy: str = ""
    # Virtual address-space cap (MiB). JIT runtimes (JVM, V8) reserve far more
    # virtual memory than they use, so they need a larger value than memory_kb.
    rlimit_as_mb: int = 0
    # Extra read-only bind mounts ("host:jail") needed by the runtime.
    extra_bindmounts_ro: tuple[str, ...] = ()
    # Lines of stdout containing any of these substrings are dropped
    # (e.g. Verilog's "$finish called at ..." trailer).
    strip_stdout_lines_containing: tuple[str, ...] = ()
    # Argument used by the readiness probe to print the toolchain version.
    version_arg: str = ""

    def build_limits(self) -> ResourceLimits:
        base = self.build.limits if self.build else ResourceLimits()
        return base.with_defaults(DEFAULT_BUILD_LIMITS)

    def run_limits(self) -> ResourceLimits:
        return self.run.limits.with_defaults(DEFAULT_RUN_LIMITS)


@dataclass
class Config:
    languages: dict[str, Language]
    max_body_bytes: int = 4 * 1024 * 1024
    max_source_bytes: int = 256 * 1024
    max_tests: int = 50
    max_concurrent: int = 0
    queue_timeout_s: int = 30


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer (got {value!r})")
    return value


def _str_list(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{where} must be a list of strings")
    return tuple(raw)


def _limits(raw: Any, where: str) -> ResourceLimits:
    if raw is None:
        return ResourceLimits()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.limits must be a mapping")
    limits = ResourceLimits(
        wall_time_s=_int(raw, "wall_time_s", 0),
        memory_kb=_int(raw, "memory_kb", 0),
        max_processes=_int(raw, "max_processes", 0),
    )
    if min(limits.wall_time_s, limits.memory_kb, limits.max_processes) < 0:
        raise ConfigError(f"{where}.limits must not be negative")
    return limits


def _phase(raw: Any, where: str) -> PhaseConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    cmd = raw.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd:
        raise ConfigError(f"{where}.cmd must be a non-empty string")
    return PhaseConfig(
        cmd=cmd,
        args=_str_list(raw.get("args"), f"{where}.args"),
        limits=_limits(raw.get("limits"), where),
        flag_allowlist=_str_list(raw.get("flag_allowlist"), f"{where}.flag_allowlist"),
    )


def _language(lang_id: str, raw: Any) -> Language:
    where = f"languages.{lang_id}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    declared_id = raw.get("id", lang_id)
    if declared_id != lang_id:
        raise ConfigError(f"{where}.id ({declared_id!r}) must match its key")
    name = raw.get("name") or ""
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}.name must be a non-empty string")
    if "run" not in raw:
        raise ConfigError(f"{where}.run is required")
    build = _phase(raw["build"], f"{where}.build") if raw.get("build") is not None else None

    def opt_str(key: str, default: str = "") -> str:
        value = raw.get(key, default)
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{key} must be a string")
        return value

    return Language(
        id=lang_id,
        name=name,
        run=_phase(raw["run"], f"{where}.run"),
        build=build,
        source_filename=opt_str("source_filename", "solution") or "solution",
        source_filename_strategy=opt_str("source_filename_strategy"),
        artifact=opt_str("artifact"),
        artifact_filename_strategy=opt_str("artifact_filename_strategy"),
        rlimit_as_mb=_int(raw, "rlimit_as_mb", 0),
        extra_bindmounts_ro=_str_list(
            raw.get("extra_bindmounts_ro"), f"{where}.extra_bindmounts_ro"
        ),
        strip_stdout_lines_containing=_str_list(
            raw.get("strip_stdout_lines_containing"), f"{where}.strip_stdout_lines_containing"
        ),
        version_arg=opt_str("version_arg"),
    )


def parse(raw: Any) -> Config:
    """Build a validated ``Config`` from already-parsed YAML data."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    langs_raw = raw.get("languages")
    if not isinstance(langs_raw, dict) or not langs_raw:
        raise ConfigError("no languages configured")

    cfg = Config(
        languages={str(k): _language(str(k), v) for k, v in langs_raw.items()},
        max_body_bytes=_int(raw, "max_body_bytes", 4 * 1024 * 1024),
        max_source_bytes=_int(raw, "max_source_bytes", 256 * 1024),
        max_tests=_int(raw, "max_tests", 50),
        max_concurrent=_int(raw, "max_concurrent", 0),
        queue_timeout_s=_int(raw, "queue_timeout_s", 30),
    )
    if cfg.max_body_bytes <= 0:
        cfg.max_body_bytes = 4 * 1024 * 1024
    if cfg.max_concurrent <= 0:
        cfg.max_concurrent = os.cpu_count() or 1
    if cfg.queue_timeout_s <= 0:
        cfg.queue_timeout_s = 30
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Fail loudly at startup on an unusable configuration."""
    if not cfg.languages:
        raise ConfigError("no languages configured")
    if cfg.max_source_bytes <= 0:
        raise ConfigError("max_source_bytes must be positive")
    if cfg.max_tests <= 0:
        raise ConfigError("max_tests must be positive")
    if cfg.max_concurrent <= 0:
        raise ConfigError(f"max_concurrent must be positive (got {cfg.max_concurrent})")


def load(path: str | Path) -> Config:
    """Read and validate a YAML configuration file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"failed to read config file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML: {exc}") from exc
    return parse(raw)
