"""Per-request jail directories, safe path handling and output capping."""

from __future__ import annotations

import itertools
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

JAIL_PREFIX = "pyboxd-"
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024  # 1 MiB per stream
TRUNCATION_MARKER = "\n[output truncated]"

_counter = itertools.count(1)


def jail_base_dir() -> Path:
    return Path(os.environ.get("PYBOXD_JAIL_DIR") or tempfile.gettempdir())


class JailDir:
    """A fresh, uniquely named directory for one request, removed on exit.

    The name combines the PID (unique across server processes), a monotonic
    counter (unique within this process) and 6 random hex chars, and the
    directory is created with ``mkdtemp`` which never reuses an existing path.
    """

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or jail_base_dir()
        self.path: Path | None = None

    def __enter__(self) -> Path:
        prefix = f"{JAIL_PREFIX}{os.getpid()}-{next(_counter)}-{secrets.token_hex(3)}-"
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.base))
        # nsjail drops privileges inside the jail, so the sandboxed user must be
        # able to write build artifacts here.
        path.chmod(0o777)  # noqa: S103
        self.path = path
        return path

    def __exit__(self, *exc: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
            if self.path.exists():
                log.error("failed to remove jail dir", extra={"path": str(self.path)})


def safe_join(base: Path, name: str) -> Path:
    """Join ``name`` onto ``base`` and guarantee the result stays inside ``base``.

    Defence in depth: names are already validated, but only the basename is
    used and the resolved result is checked against the base.
    """
    base_abs = base.resolve()
    joined = (base_abs / os.path.basename(name)).absolute()
    if joined.parent != base_abs or joined.name in ("", ".", ".."):
        raise ValueError(f"path {name!r} escapes sandbox")
    return joined


def write_source(path: Path, source: str) -> None:
    """Create the source file without following symlinks (prevents TOCTOU tricks)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(source)
    if not path.is_file() or path.is_symlink():
        raise OSError("source file is not a regular file")


def cap_output(data: bytes, limit: int, truncated: bool) -> str:
    """Decode captured output, appending the truncation marker if it was cut."""
    text = data[:limit].decode("utf-8", errors="replace")
    return text + TRUNCATION_MARKER if truncated or len(data) > limit else text


def sweep_orphaned_dirs(base: Path | None = None, max_age_s: float = 600) -> int:
    """Remove ``pyboxd-*`` directories older than ``max_age_s``.

    Age-by-mtime is used instead of matching live PIDs: PIDs get reused, and no
    legitimate run can outlive its wall-time limit by minutes anyway.
    Returns the number of directories removed.
    """
    base = base or jail_base_dir()
    removed = 0
    try:
        entries = list(os.scandir(base))
    except OSError as exc:
        log.warning("sweep: cannot read base directory", extra={"base": str(base), "err": str(exc)})
        return 0
    now = time.time()
    for entry in entries:
        if not entry.name.startswith(JAIL_PREFIX):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime <= max_age_s:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if not os.path.exists(entry.path):
            removed += 1
            log.info("sweep: removed orphaned directory", extra={"path": entry.path})
    return removed
