from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyboxd.app import Settings, create_app
from pyboxd.config import Config, load, parse
from pyboxd.runner import SandboxMode

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def repo_config() -> Config:
    return load(ROOT / "languages.yaml")


@pytest.fixture
def small_config() -> Config:
    return parse(
        {
            "languages": {
                "py3": {
                    "name": "Python 3",
                    "source_filename": "solution.py",
                    "run": {
                        "cmd": "/usr/bin/python3",
                        "args": ["{{source}}"],
                        "limits": {"wall_time_s": 5, "memory_kb": 262144, "max_processes": 16},
                    },
                },
                "c": {
                    "name": "C",
                    "source_filename": "solution.c",
                    "artifact": "solution",
                    "build": {
                        "cmd": "/usr/bin/gcc",
                        "args": ["{{flags}}", "-o", "{{artifact}}", "{{source}}"],
                        "limits": {"wall_time_s": 10, "memory_kb": 1048576, "max_processes": 50},
                        "flag_allowlist": ["-O2", "-std=*"],
                    },
                    "run": {"cmd": "./{{artifact}}"},
                },
            },
            "max_source_bytes": 1000,
            "max_body_bytes": 4000,
            "max_tests": 3,
            "max_concurrent": 1,
            "queue_timeout_s": 1,
        }
    )


def make_client(cfg: Config, mode: SandboxMode = SandboxMode.UNSANDBOXED) -> TestClient:
    app = create_app(
        Settings(config=cfg, auth_token=TOKEN, sandbox_mode=mode, background_tasks=False)
    )
    return TestClient(app)


@pytest.fixture
def client(small_config: Config) -> TestClient:
    with make_client(small_config) as c:
        yield c
