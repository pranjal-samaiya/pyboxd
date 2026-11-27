# Architecture and security model

## Modules

| Module        | Responsibility                                                                |
|---------------|-------------------------------------------------------------------------------|
| `app.py`      | FastAPI routes, auth dependency, request logging, semaphore, background tasks |
| `auth.py`     | Bearer-token check with `hmac.compare_digest`                                 |
| `models.py`   | Strict Pydantic request schema (`extra="forbid"`, `strict=True`)              |
| `validate.py` | Filename and flag-allowlist validation                                        |
| `config.py`   | YAML language registry, limits, `merge_with_cap`                              |
| `runner.py`   | Async subprocess execution, output capping, verdict mapping                   |
| `nsjail.py`   | nsjail argument construction and seccomp policy                               |
| `sandbox.py`  | Jail directories, `safe_join`, `O_NOFOLLOW` writes, orphan sweeper            |
| `probe.py`    | Readiness probes for nsjail and each toolchain                                |
| `status.py`   | Verdict vocabulary and aggregation rules                                      |

## Request lifecycle

1. **Auth.** Every request except `/healthz` must carry the bearer token.
2. **Body cap.** The body is streamed and rejected as soon as it passes `max_body_bytes`.
3. **Schema.** The strict Pydantic model rejects unknown fields and wrong types.
4. **Validation.** This step checks the language, source size, filenames, flags,
   limits (which can only go down) and the test count.
5. **Queue.** `asyncio.wait_for(semaphore.acquire(), queue_timeout_s)` either
   gets a slot or returns `429`.
6. **Jail directory.** The directory is named `pyboxd-<pid>-<counter>-<6 hex>`
   and created with `mkdtemp`, so it is never reused. It is removed in a
   `finally` block, and the sweeper deletes leftovers after 10 minutes.
7. **Build** (compiled languages). nsjail runs with the jail directory as a
   **writable** root and a private tmpfs `/tmp`.
8. **Run.** Each test case runs in a new nsjail with the jail directory as a
   **read-only** root. Stdin is streamed in, stdout and stderr are drained
   concurrently with a 1 MiB cap, and a watchdog kills the process group at
   `wall_time_s + 2` seconds.
9. **Verdict.** The checks run in this order: timeout or SIGXCPU, OOM, non-zero
   exit, then output comparison.

## Isolation layers

| Layer       | What it stops                                                                                     |
|-------------|---------------------------------------------------------------------------------------------------|
| Namespaces  | User, PID, mount, network, IPC and UTS: no network, no view of host processes                     |
| Filesystem  | Only the request's files plus read-only `/usr`, `/lib`, `/lib64`, `/bin`, `/etc`                   |
| Credentials | Runs as host uid/gid 65534 (`--user 65534:65534:1`), never root mapped into a namespace           |
| seccomp     | Kills on `ptrace`, `mount`, `unshare`, `setns`, `bpf`, `kexec_load`, `keyctl`, `userfaultfd` and more |
| rlimits     | CPU time, address space, process count, file size (100 MB), open files (1024), no core dumps      |
| cgroup v2   | Memory accounting (`memory.peak`, OOM kills) when available                                       |
| Wall clock  | nsjail `--time_limit` plus an asyncio watchdog that `killpg`s the process group                   |

### Hardening beyond goboxd

- **Build phase isolation.** goboxd compiled with the host `/` as the chroot,
  so `#include "/any/host/file"` could read host files or another request's
  source. PyBoxD's build phase sees only the jail directory and read-only system
  directories. `tests/test_e2e.py::test_build_cannot_read_host_files` covers this.
- **Unprivileged uid.** By default nsjail maps the jailed uid to the uid of the
  caller, which is root in the container. PyBoxD maps it to host `nobody`, so
  root-only files under the read-only `/etc` (such as `/etc/shadow`) cannot be read.
- **Fail-safe cgroups.** If nsjail cannot start with cgroup v2 on a host, PyBoxD
  disables cgroup accounting once and retries, instead of failing every job.
- **No silent unsandboxed fallback.** Without nsjail, `/run` returns `503`
  unless `PYBOXD_ALLOW_UNSANDBOXED=1` is set explicitly.

## Why these choices

- **`asyncio.Semaphore` instead of a worker pool.** Uvicorn already runs one
  coroutine per request, and a semaphore with a timed `acquire` gives bounded
  concurrency and backpressure without extra queues or workers.
- **Directory age instead of PID matching in the sweeper.** PIDs get reused. A
  jail directory older than 10 minutes has outlived every wall-time limit, so it
  must be orphaned.
- **YAML registry.** Language behaviour lives in data: templates, limits,
  allowlists, `rlimit_as_mb` for JIT runtimes, extra mounts and output filters.
  Adding a language never touches Python.
