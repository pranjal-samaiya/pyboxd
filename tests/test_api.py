"""HTTP contract: auth, validation errors, queueing and response shapes.

The runner is replaced with a stub here, so these tests need no toolchains.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pyboxd import app as app_module
from pyboxd.app import Settings, create_app
from pyboxd.auth import AuthConfigError
from pyboxd.config import Config
from pyboxd.runner import RunResult, SandboxMode, TestResult

from .conftest import AUTH, TOKEN, make_client

PY_JOB = {"language": "py3", "source": "print(1)", "tests": [{"expected_stdout": "1\n"}]}


@pytest.fixture(autouse=True)
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    async def fake_run(lang: Any, req: Any, mode: Any) -> RunResult:
        calls.append(req)
        return RunResult(
            status="accepted",
            build=None,
            tests=[TestResult(status="accepted", stdout="1\n") for _ in req.tests],
        )

    monkeypatch.setattr(app_module, "run_sandbox", fake_run)
    return calls


def err(resp: httpx.Response) -> str:
    return str(resp.json()["error"]["code"])


# ---------------------------------------------------------------- auth


def test_token_must_be_configured(small_config: Config) -> None:
    for bad in ("", "short"):
        with pytest.raises(AuthConfigError):
            create_app(Settings(config=small_config, auth_token=bad))


def test_healthz_is_public(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/readyz"), ("GET", "/info"), ("POST", "/run")]
)
@pytest.mark.parametrize(
    "header",
    [
        None,
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer wrong-token-value-xxxxxxxx",
        f"Bearer {TOKEN}-extra",
        f"Bearer {TOKEN[:-1]}",
        TOKEN,
    ],
)
def test_protected_routes_reject_bad_auth(
    client: TestClient, method: str, path: str, header: str | None, stub_runner: list[Any]
) -> None:
    headers = {"Authorization": header} if header is not None else {}
    r = client.request(method, path, headers=headers, json=PY_JOB)
    assert r.status_code == 401
    assert err(r) == "unauthorized"
    assert r.headers["WWW-Authenticate"].startswith("Bearer")
    assert stub_runner == []


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    r = client.post("/run", headers={"Authorization": f"bearer {TOKEN}"}, json=PY_JOB)
    assert r.status_code == 200


def test_cors_preflight_allows_authorization(client: TestClient) -> None:
    r = client.options(
        "/run",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


# ---------------------------------------------------------------- /run happy path


def test_run_ok(client: TestClient, stub_runner: list[Any]) -> None:
    job = {
        "language": "c",
        "source": "int main(){}",
        "build": {"flags": ["-O2", "-std=c11"], "limits": {"wall_time_s": 5}},
        "run": {"limits": {"memory_kb": 1024}},
        "tests": [{"stdin": "", "expected_stdout": "1\n"}],
    }
    r = client.post("/run", headers=AUTH, json=job)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["tests"][0] == {
        "status": "accepted",
        "stdout": "1\n",
        "stderr": "",
        "duration_ms": 0,
        "memory_peak_kb": 0,
    }
    assert "build" not in body  # omitted when no build ran
    req = stub_runner[0]
    assert req.build.flags == ["-O2", "-std=c11"]
    assert req.build.limits.wall_time_s == 5
    assert req.run.limits.memory_kb == 1024


# ---------------------------------------------------------------- /run validation


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("{not json", "invalid_json"),
        ("[]", "invalid_json"),
        (json.dumps({**PY_JOB, "unknown_field": 1}), "invalid_json"),
        (json.dumps({**PY_JOB, "tests": [{"stdin": "", "expected": "x"}]}), "invalid_json"),
        (json.dumps({**PY_JOB, "source": 123}), "invalid_json"),
        (json.dumps({**PY_JOB, "run": {"limits": {"wall_time_s": "2"}}}), "invalid_json"),
        (json.dumps({**PY_JOB, "language": "cobol"}), "unknown_language"),
        (json.dumps({**PY_JOB, "source": "x" * 1001}), "source_too_large"),
        (json.dumps({**PY_JOB, "source_filename": "../evil.py"}), "invalid_filename"),
        (json.dumps({**PY_JOB, "artifact_filename": ".hidden"}), "invalid_filename"),
        (json.dumps({**PY_JOB, "build": {"flags": ["-O2"]}}), "disallowed_flag"),
        (json.dumps({**PY_JOB, "run": {"flags": ["-c"]}}), "disallowed_flag"),
        (
            json.dumps({**PY_JOB, "language": "c", "build": {"flags": ["-fplugin=x.so"]}}),
            "disallowed_flag",
        ),
        (json.dumps({**PY_JOB, "run": {"limits": {"wall_time_s": 99}}}), "invalid_limits"),
        (json.dumps({**PY_JOB, "run": {"limits": {"memory_kb": -5}}}), "invalid_limits"),
        (json.dumps({**PY_JOB, "build": {"limits": {"wall_time_s": 1}}}), "invalid_limits"),
        (json.dumps({**PY_JOB, "tests": []}), "invalid_test_count"),
        (json.dumps({**PY_JOB, "tests": [{}] * 4}), "invalid_test_count"),
    ],
)
def test_run_rejects_bad_requests(
    client: TestClient, payload: str, code: str, stub_runner: list[Any]
) -> None:
    r = client.post("/run", headers={**AUTH, "Content-Type": "application/json"}, content=payload)
    assert r.status_code == 400, r.text
    assert err(r) == code
    assert r.json()["error"]["message"]
    assert stub_runner == []


def test_body_too_large(client: TestClient) -> None:
    r = client.post("/run", headers=AUTH, content=b"x" * 5000)
    assert r.status_code == 400 and err(r) == "body_too_large"


def test_sandbox_unavailable(small_config: Config) -> None:
    with make_client(small_config, SandboxMode.UNAVAILABLE) as c:
        r = c.post("/run", headers=AUTH, json=PY_JOB)
    assert r.status_code == 503 and err(r) == "sandbox_unavailable"


def test_internal_error_is_500_and_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: Any) -> RunResult:
        raise app_module.SandboxError("nsjail exploded")

    monkeypatch.setattr(app_module, "run_sandbox", boom)
    r = client.post("/run", headers=AUTH, json=PY_JOB)
    assert r.status_code == 500 and err(r) == "internal_error"
    stats = client.get("/info", headers=AUTH).json()["stats"]
    assert stats["jobs_failed_internal"] == 1
    assert stats["last_internal_error_at"]
    assert stats["in_flight_jobs"] == 0


# ---------------------------------------------------------------- concurrency


def test_queue_full_returns_429(small_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_concurrent=1, queue_timeout_s=1: a second job waiting >1s gets 429."""
    release = asyncio.Event()

    async def slow(lang: Any, req: Any, mode: Any) -> RunResult:
        await release.wait()
        return RunResult(status="accepted", build=None, tests=[TestResult(status="accepted")])

    monkeypatch.setattr(app_module, "run_sandbox", slow)
    app = create_app(
        Settings(
            config=small_config,
            auth_token=TOKEN,
            sandbox_mode=SandboxMode.UNSANDBOXED,
            background_tasks=False,
        )
    )

    async def scenario() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            first = asyncio.create_task(ac.post("/run", headers=AUTH, json=PY_JOB))
            await asyncio.sleep(0.1)
            second = await ac.post("/run", headers=AUTH, json=PY_JOB)
            release.set()
            return (await first).status_code, second.status_code

    first_status, second_status = asyncio.run(scenario())
    assert first_status == 200
    assert second_status == 429


# ---------------------------------------------------------------- info / readyz


def test_info_shape(client: TestClient) -> None:
    body = client.get("/info", headers=AUTH).json()
    assert set(body) == {"build_info", "sandbox_mode", "nsjail", "languages", "limits", "stats"}
    assert body["build_info"]["python_version"]
    assert [lang["id"] for lang in body["languages"]] == ["c", "py3"]
    assert body["limits"] == {"max_source_bytes": 1000, "max_tests": 3, "max_concurrent_jobs": 1}
    assert body["languages"][1]["default_run_limits"] == {
        "wall_time_s": 5,
        "memory_kb": 262144,
        "max_processes": 16,
    }


def test_readyz_degraded_without_probes(client: TestClient) -> None:
    r = client.get("/readyz", headers=AUTH)
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert set(body["languages"]) == {"c", "py3"}


def test_root_redirects_to_info(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/info"
