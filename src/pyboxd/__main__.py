"""Command-line entry point: ``pyboxd --config languages.yaml --port 8080``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from . import __version__, auth
from .app import Settings, create_app
from .config import ConfigError, load
from .logs import setup_logging

log = logging.getLogger("pyboxd")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyboxd", description="Sandboxed code execution server")
    parser.add_argument("--config", default=os.environ.get("PYBOXD_CONFIG", "languages.yaml"))
    parser.add_argument("--host", default=os.environ.get("PYBOXD_HOST", "0.0.0.0"))  # noqa: S104
    parser.add_argument("--port", type=int, default=int(os.environ.get("PYBOXD_PORT", "8080")))
    parser.add_argument("--log-level", default=os.environ.get("PYBOXD_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        log.error("invalid configuration", extra={"err": str(exc), "path": args.config})
        return 1

    env_conc = os.environ.get("PYBOXD_MAX_CONCURRENT", "")
    if env_conc.isdigit() and int(env_conc) > 0:
        cfg.max_concurrent = int(env_conc)

    origins = [
        o.strip() for o in os.environ.get("PYBOXD_CORS_ORIGINS", "*").split(",") if o.strip()
    ]
    try:
        app = create_app(
            Settings(
                config=cfg,
                auth_token=auth.check_token_config(os.environ.get(auth.TOKEN_ENV)),
                version=os.environ.get("PYBOXD_VERSION", __version__),
                commit=os.environ.get("PYBOXD_COMMIT", "unknown"),
                cors_origins=origins,
            )
        )
    except auth.AuthConfigError as exc:
        log.error(str(exc))
        return 1

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
        timeout_keep_alive=60,
        # Bound header size and open connections (slowloris mitigation). Jobs beyond
        # max_concurrent wait in the asyncio queue and get 429 after queue_timeout_s.
        h11_max_incomplete_event_size=16 * 1024,
        limit_concurrency=int(os.environ.get("PYBOXD_MAX_CONNECTIONS", "1024")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
