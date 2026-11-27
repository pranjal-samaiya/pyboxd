"""End-to-end: real compilers and interpreters.

Runs inside nsjail when it is installed (the Docker image, or a Linux host with
nsjail), otherwise in unsandboxed dev mode so CI still exercises the runner.
Isolation tests only run under nsjail.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pyboxd.config import Config, parse
from pyboxd.nsjail import nsjail_available
from pyboxd.runner import SandboxMode

from .conftest import AUTH, make_client

pytestmark = pytest.mark.e2e

MODE = SandboxMode.NSJAIL if nsjail_available() else SandboxMode.UNSANDBOXED
needs_nsjail = pytest.mark.skipif(MODE is not SandboxMode.NSJAIL, reason="nsjail not installed")


def _which(name: str) -> str | None:
    path = shutil.which(name)
    # Under nsjail only /usr, /bin and /lib are visible inside the jail.
    if path and MODE is SandboxMode.NSJAIL and not path.startswith(("/usr/", "/bin/")):
        return None
    return path


PY = _which("python3")
GCC = _which("gcc")
JAVAC = _which("javac")
JAVA = _which("java")


def _config() -> Config:
    langs: dict[str, Any] = {}
    if PY:
        langs["py3"] = {
            "name": "Python 3",
            "source_filename": "solution.py",
            "run": {
                "cmd": PY,
                "args": ["{{source}}"],
                "limits": {"wall_time_s": 3, "memory_kb": 262144, "max_processes": 32},
            },
        }
    if GCC:
        langs["c"] = {
            "name": "C",
            "source_filename": "solution.c",
            "artifact": "solution",
            "build": {
                "cmd": GCC,
                "args": ["{{flags}}", "-o", "{{artifact}}", "{{source}}"],
                "limits": {"wall_time_s": 20, "memory_kb": 1048576, "max_processes": 100},
                "flag_allowlist": ["-O2"],
            },
            "run": {"cmd": "./{{artifact}}", "limits": {"wall_time_s": 2}},
        }
    if JAVAC and JAVA:
        langs["java"] = {
            "name": "Java",
            "source_filename": "Solution.java",
            "source_filename_strategy": "from_request",
            "artifact": "Solution.class",
            "artifact_filename_strategy": "from_request",
            "rlimit_as_mb": 4096,
            "build": {"cmd": JAVAC, "args": ["{{source}}"], "limits": {"wall_time_s": 60}},
            "run": {
                "cmd": JAVA,
                "args": ["-Xmx128m", "-cp", ".", "{{class}}"],
                "limits": {"wall_time_s": 10, "memory_kb": 524288, "max_processes": 100},
            },
        }
    if not langs:
        pytest.skip("no toolchains available")
    return parse({"languages": langs, "max_concurrent": 2})


@pytest.fixture(scope="module")
def e2e() -> TestClient:
    with make_client(_config(), MODE) as c:
        yield c


def run(client: TestClient, lang: str, source: str, *tests: tuple[str, str], **extra: Any) -> Any:
    job = {
        "language": lang,
        "source": source,
        "tests": [{"stdin": i, "expected_stdout": o} for i, o in tests] or [{}],
        **extra,
    }
    r = client.post("/run", headers=AUTH, json=job)
    assert r.status_code == 200, r.text
    return r.json()


needs_py = pytest.mark.skipif(not PY, reason="python3 not available")
needs_gcc = pytest.mark.skipif(not GCC, reason="gcc not available")

SUM_PY = "a, b = map(int, input().split())\nprint(a + b)\n"
SUM_C = (
    "#include <stdio.h>\n"
    'int main(){int a,b;if(scanf("%d %d",&a,&b)!=2)return 1;printf("%d\\n",a+b);return 0;}\n'
)


@needs_py
def test_python_accepted(e2e: TestClient) -> None:
    body = run(e2e, "py3", SUM_PY, ("1 2", "3\n"), ("10 -4", "6\n"))
    assert body["status"] == "accepted"
    assert [t["status"] for t in body["tests"]] == ["accepted", "accepted"]
    assert "build" not in body


@needs_py
@pytest.mark.parametrize(
    ("source", "expected", "verdict"),
    [
        ("print(41)", "42\n", "wrong_output"),
        ("print(' 42 ')", "42\n", "output_whitespace_mismatch"),
        ("raise SystemExit(3)", "", "runtime_error"),
        ("import sys; sys.stderr.write('boom'); 1/0", "", "runtime_error"),
        ("while True: pass", "", "time_exceeded"),
        ("import time; time.sleep(30)", "", "time_exceeded"),
        ("x = bytearray(2 * 1024 ** 3)", "", "memory_exceeded"),
    ],
)
def test_python_verdicts(e2e: TestClient, source: str, expected: str, verdict: str) -> None:
    body = run(e2e, "py3", source, ("", expected))
    assert body["tests"][0]["status"] == verdict, body
    assert body["status"] == verdict


@needs_py
def test_first_failing_test_sets_top_level_status(e2e: TestClient) -> None:
    body = run(e2e, "py3", "print(input())", ("a", "a\n"), ("b", "x\n"), ("c", "c\n"))
    assert [t["status"] for t in body["tests"]] == ["accepted", "wrong_output", "accepted"]
    assert body["status"] == "wrong_output"


@needs_py
def test_output_is_capped(e2e: TestClient) -> None:
    body = run(e2e, "py3", "import sys\nsys.stdout.write('A' * 3_000_000)", ("", ""))
    out = body["tests"][0]["stdout"]
    assert out.endswith("[output truncated]")
    assert len(out) < 1_100_000


@needs_py
def test_stdin_is_delivered_and_large_stdin_ok(e2e: TestClient) -> None:
    data = "x" * 200_000
    body = run(e2e, "py3", "import sys; print(len(sys.stdin.read()))", (data, "200000\n"))
    assert body["status"] == "accepted"


@needs_gcc
def test_c_accepted_with_flag(e2e: TestClient) -> None:
    body = run(e2e, "c", SUM_C, ("3 4", "7\n"), build={"flags": ["-O2"]})
    assert body["status"] == "accepted", body
    assert body["build"]["status"] == "ok"


@needs_gcc
def test_c_build_failure(e2e: TestClient) -> None:
    body = run(e2e, "c", "int main( {", ("", ""), ("", ""))
    assert body["status"] == "build_failed"
    assert body["build"]["status"] == "failed"
    assert "error" in body["build"]["stderr"]
    assert [t["status"] for t in body["tests"]] == ["not_executed", "not_executed"]


@needs_gcc
def test_c_segfault_is_runtime_error(e2e: TestClient) -> None:
    body = run(e2e, "c", "int main(){volatile int*p=0;return *p;}", ("", ""))
    assert body["tests"][0]["status"] == "runtime_error"


@pytest.mark.skipif(not (JAVAC and JAVA), reason="java not available")
def test_java_custom_class_name(e2e: TestClient) -> None:
    src = 'public class Main { public static void main(String[] a){ System.out.println("hi"); } }'
    body = run(e2e, "java", src, ("", "hi\n"), source_filename="Main.java")
    assert body["status"] == "accepted", body


# ---------------------------------------------------------------- isolation (nsjail only)


@needs_nsjail
@needs_py
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # runs as an unprivileged user
        ("import os; print(os.getuid())", "65534\n"),
        # host secrets are unreadable
        (
            "try:\n open('/etc/shadow').read(); print('LEAK')\n"
            "except PermissionError: print('denied')",
            "denied\n",
        ),
        # host filesystem outside the read-only system dirs is invisible
        ("import os; print(os.path.exists('/home'), os.path.exists('/root'))", "False False\n"),
        # the jail root is read-only during the run phase
        (
            "try:\n open('/evil','w'); print('WROTE')\nexcept OSError: print('ro')",
            "ro\n",
        ),
        # no network
        (
            "import socket\ntry:\n socket.create_connection(('1.1.1.1', 80), timeout=2);"
            " print('NET')\nexcept OSError: print('nonet')",
            "nonet\n",
        ),
    ],
)
def test_isolation(e2e: TestClient, source: str, expected: str) -> None:
    body = run(e2e, "py3", source, ("", expected))
    assert body["status"] == "accepted", body["tests"][0]


@needs_nsjail
@needs_py
def test_seccomp_kills_ptrace(e2e: TestClient) -> None:
    src = "import ctypes; ctypes.CDLL(None).ptrace(0, 0, 0, 0); print('survived')"
    body = run(e2e, "py3", src, ("", "survived\n"))
    assert body["tests"][0]["status"] == "runtime_error"


@needs_nsjail
@needs_py
def test_fork_bomb_is_contained(e2e: TestClient) -> None:
    src = "import os\nwhile True:\n    try: os.fork()\n    except OSError: pass"
    body = run(e2e, "py3", src, ("", ""))
    assert body["tests"][0]["status"] in ("time_exceeded", "runtime_error")


@needs_nsjail
@needs_gcc
def test_build_cannot_read_host_files(e2e: TestClient, tmp_path_factory: Any) -> None:
    # A valid header that exists on the host but outside the jail.
    secret = tmp_path_factory.mktemp("host") / "secret.h"
    secret.write_text("int secret = 42;\n")
    secret.chmod(0o644)
    body = run(e2e, "c", f'#include "{secret}"\nint main(){{return secret;}}', ("", ""))
    assert body["status"] == "build_failed"
    assert "No such file" in body["build"]["stderr"]


@needs_nsjail
@needs_py
def test_broken_cgroups_fall_back_to_rlimits(
    e2e: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """If nsjail cannot use cgroups on this host, jobs still run (rlimits only)."""
    from pyboxd import runner

    monkeypatch.setattr(runner, "CGROUP_ROOT", tmp_path)  # fake, unusable cgroupfs
    monkeypatch.setattr(runner, "_cgroups_failed", False)
    monkeypatch.setenv("PYBOXD_CGROUPS", "auto")
    (tmp_path / "cgroup.controllers").write_text("cpu memory pids\n")
    body = run(e2e, "py3", "print(42)", ("", "42\n"))
    assert body["status"] == "accepted", body
    assert runner._cgroups_failed is True
