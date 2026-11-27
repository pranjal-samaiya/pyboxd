# PyBoxD

**Secure sandboxed code execution service in Python.** PyBoxD compiles and runs
untrusted code inside [nsjail](https://github.com/google/nsjail) and returns a
judge-style verdict for each test case, like the backends behind Codeforces or
LeetCode. It is built with FastAPI and asyncio.

```bash
curl -s localhost:8080/run \
  -H "Authorization: Bearer $PYBOXD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"language": "py3",
       "source": "a, b = map(int, input().split())\nprint(a + b)",
       "tests": [{"stdin": "2 3", "expected_stdout": "5\n"}]}'
```

```json
{"status": "accepted",
 "tests": [{"status": "accepted", "stdout": "5\n", "stderr": "", "duration_ms": 21, "memory_peak_kb": 0}]}
```

## Features

- **8 languages out of the box.** Python 3, C, C++, Java, JavaScript (Node.js), Bash,
  Verilog and Rust. Languages are defined in [`languages.yaml`](languages.yaml), so
  adding one is a YAML block plus a one-line install script, with no Python changes.
- **Defence-in-depth sandbox.** Every build and every test case runs in fresh
  Linux namespaces with:
  - no network
  - a read-only root containing only the request's own files plus read-only `/usr`, `/lib`, `/bin`, `/etc`
  - an unprivileged uid (`nobody`)
  - a seccomp policy that kills on `ptrace`, `mount`, `unshare`, `bpf` and similar syscalls
  - rlimits on CPU time, address space, processes, file size and open files
  - cgroup v2 memory accounting where available
- **Bounded concurrency.** An `asyncio.Semaphore` caps how many jobs run at once. Extra
  requests wait in a queue and get `429 queue_full` after `queue_timeout_s`.
  Child processes are async subprocesses, so the event loop never blocks.
- **Bearer-token auth.** Every endpoint except `/healthz` requires
  `Authorization: Bearer <token>`, checked with `hmac.compare_digest` (constant time).
  The server refuses to start without a token of at least 16 characters.
- **Hardened input handling.** Strict request schema (unknown fields rejected), filename
  validation against traversal, compiler-flag allowlists with glob support, separate
  body-size and source-size caps, per-request limits that can only lower the defaults,
  1 MiB output cap per stream, and `O_NOFOLLOW` source writes.
- **Operability.** Structured JSON logs with a request ID per request, `/readyz` with
  per-toolchain version probes refreshed every 60 s, `/info` with live stats, and a
  background sweeper for orphaned jail directories.

## Quick start (Docker)

```bash
cp .env.example .env          # then put a long random secret in PYBOXD_AUTH_TOKEN
docker compose up --build -d
source .env
curl -s -H "Authorization: Bearer $PYBOXD_AUTH_TOKEN" localhost:8080/readyz
python3 scripts/smoke_test.py   # runs one program per language
```

nsjail needs to create namespaces, so the container runs `privileged` with the
host cgroup namespace (see [`docker-compose.yml`](docker-compose.yml)).

## API

| Method | Path       | Auth | Purpose                                                       |
|--------|------------|------|---------------------------------------------------------------|
| GET    | `/healthz` | no   | Liveness: `{"status":"ok"}`                                   |
| GET    | `/readyz`  | yes  | 200 if nsjail and every toolchain respond, else 503           |
| GET    | `/info`    | yes  | Version, languages and default limits, global limits, stats   |
| POST   | `/run`     | yes  | Build once, run every test case, return verdicts              |

**Verdicts:** `accepted`, `wrong_output`, `output_whitespace_mismatch` (only
leading or trailing whitespace differs), `time_exceeded`, `memory_exceeded`,
`runtime_error`, `build_failed` (tests become `not_executed`), `internal_error`.
The top-level status is `accepted` only if every test passed. Otherwise it is
the first non-accepted verdict.

A failure in the user's code is always HTTP 200 with a verdict. 4xx means the
request was rejected and 5xx means the sandbox itself failed. Full request and
response reference: [`docs/api.md`](docs/api.md).

## Configuration

| Environment variable       | Default                    | Meaning                                              |
|----------------------------|----------------------------|------------------------------------------------------|
| `PYBOXD_AUTH_TOKEN`        | *(required)*               | Bearer token, at least 16 characters                 |
| `PYBOXD_CONFIG`            | `languages.yaml`           | Language registry and global limits                  |
| `PYBOXD_PORT` / `_HOST`    | `8080` / `0.0.0.0`         | Listen address                                       |
| `PYBOXD_MAX_CONCURRENT`    | `max_concurrent` or CPUs   | Jobs that may run at the same time                   |
| `PYBOXD_CORS_ORIGINS`      | `*`                        | Comma-separated allowed origins                      |
| `PYBOXD_JAIL_DIR`          | system temp dir            | Where per-request jail directories are created       |
| `PYBOXD_SANDBOX_UID`       | `65534`                    | Host uid/gid the jailed program runs as              |
| `PYBOXD_CGROUPS`           | `auto`                     | `off` disables cgroup v2 memory accounting           |
| `PYBOXD_MAX_CONNECTIONS`   | `1024`                     | Open-connection cap (uvicorn)                        |
| `NSJAIL_PATH`              | `/usr/sbin/nsjail`         | nsjail binary                                        |
| `PYBOXD_ALLOW_UNSANDBOXED` | unset                      | `1` runs code **without isolation** (dev/CI only)    |

### Adding a language

1. Add a block to `languages.yaml`:

   ```yaml
   lua:
     name: Lua 5.4
     source_filename: solution.lua
     run:
       cmd: /usr/bin/lua5.4
       args: ["{{source}}"]
       limits: { wall_time_s: 5, memory_kb: 262144, max_processes: 16 }
   ```

2. Add `scripts/lang_install/lua.sh` containing
   `apt-get install -y --no-install-recommends lua5.4`.

The available templates are `{{source}}`, `{{artifact}}`, `{{class}}` and `{{flags}}`.
Optional keys are `build`, `flag_allowlist`, `rlimit_as_mb` (for JIT runtimes),
`extra_bindmounts_ro`, `strip_stdout_lines_containing` and `version_arg`.

## How it works

```
POST /run ─► auth ─► strict schema ─► validation ─► semaphore (queue, 429 on timeout)
                                                          │
                       fresh jail dir  pyboxd-<pid>-<n>-<rand>
                                                          │
             build (nsjail, jail dir is rw root, private /tmp) ──fail──► build_failed
                                                          │ ok
             for each test: run (nsjail, jail dir is ro root) ─► verdict
                                                          │
                                         jail dir removed ─► 200 + verdicts
```

See [`docs/architecture.md`](docs/architecture.md) for the security model and design notes.

## Development

```bash
pip install -e ".[dev]"
make test        # 150+ tests; e2e tests use nsjail when installed
make lint typecheck
PYBOXD_AUTH_TOKEN=dev-token-0123456789 make run   # local server WITHOUT isolation
```

Without nsjail the end-to-end tests run in unsandboxed dev mode, and the
isolation tests (uid, `/etc/shadow`, host filesystem, network, seccomp, fork
bomb) are skipped. CI also builds the Docker image and smoke-tests every
language inside nsjail.

### Load test

`scripts/load_test.py` fires N simultaneous `/run` requests at a live server.
Here are the results on a 2-vCPU VM with `max_concurrent=2`, where every request
runs a real program in nsjail:

| Concurrent requests | Language  | Result         | Wall time |
|---------------------|-----------|----------------|-----------|
| 50                  | Python 3  | 50/50 accepted | 0.8 s     |
| 100                 | Python 3  | 100/100        | 1.4 s     |
| 200                 | Python 3  | 200/200        | 3.2 s     |
| 50                  | C++ (compile + run) | 50/50 | 9.7 s     |

## Credits and license

PyBoxD is a Python port of [goboxd](https://github.com/Praneeth0910/goboxd), a
Go sandbox execution server. It keeps goboxd's API contract and tightens the
sandbox:

- The build phase no longer sees the host filesystem.
- Jailed code runs as host `nobody` instead of root.
- A broken cgroup setup falls back to rlimits instead of failing jobs.

Licensed under the GNU GPL v3, the same license as goboxd. See [LICENSE](LICENSE).
