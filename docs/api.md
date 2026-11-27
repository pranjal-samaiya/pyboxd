# PyBoxD API reference

Base URL: `http://localhost:8080`. Every endpoint except `/healthz` needs:

```
Authorization: Bearer <PYBOXD_AUTH_TOKEN>
```

Errors share one shape:

```json
{"error": {"code": "unknown_language", "message": "language 'cobol' is not configured"}}
```

## GET /healthz

Liveness probe. No auth. Always `200 {"status": "ok"}` while the process is up.

## GET /readyz

`200` when nsjail and every configured toolchain answered their last probe,
otherwise `503`. Probes are refreshed every 60 seconds.

```json
{
  "status": "ok",
  "sandbox_mode": "nsjail",
  "nsjail": {"ok": true, "version": "3.4"},
  "languages": {
    "py3": {"ok": true, "version": "Python 3.13.5"},
    "rust": {"ok": false, "error": "[Errno 2] No such file or directory: '/usr/bin/rustc'"}
  }
}
```

`sandbox_mode` is `nsjail`, `unsandboxed` (dev mode) or `unavailable`.

## GET /info

Always `200`.

```json
{
  "build_info": {"version": "0.1.0", "commit": "a1b2c3d", "python_version": "3.13.5"},
  "sandbox_mode": "nsjail",
  "nsjail": {"path": "/usr/sbin/nsjail", "version": "3.4"},
  "languages": [
    {"id": "c", "name": "C", "version": "gcc (Debian 14.2.0-19) 14.2.0",
     "default_run_limits": {"wall_time_s": 3, "memory_kb": 524288, "max_processes": 64}}
  ],
  "limits": {"max_source_bytes": 262144, "max_tests": 50, "max_concurrent_jobs": 4},
  "stats": {"in_flight_jobs": 0, "jobs_total": 12, "jobs_failed_internal": 0,
            "disk_free_bytes_jail_dir": 51234567890}
}
```

`stats.last_internal_error_at` (ISO 8601) appears once an internal error has occurred.

## POST /run

### Request

```json
{
  "language": "cpp",
  "source": "#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<\"\\n\";}",
  "source_filename": "",
  "artifact_filename": "",
  "build": {"flags": ["-O2", "-std=c++17"], "limits": {"wall_time_s": 5}},
  "run":   {"flags": [], "limits": {"wall_time_s": 1, "memory_kb": 65536, "max_processes": 1}},
  "tests": [
    {"stdin": "2 3", "expected_stdout": "5\n"},
    {"stdin": "10 -4", "expected_stdout": "6\n"}
  ]
}
```

| Field                         | Required | Notes                                                                       |
|-------------------------------|----------|-----------------------------------------------------------------------------|
| `language`                    | yes      | An id from `languages.yaml` (see `/info`)                                   |
| `source`                      | yes      | At most `max_source_bytes` (UTF-8 bytes)                                    |
| `tests`                       | yes      | 1 to `max_tests` items. `stdin` and `expected_stdout` default to `""`       |
| `source_filename`             | no       | Used only by languages with `source_filename_strategy: from_request` (Java) |
| `artifact_filename`           | no       | Same, for `artifact_filename_strategy: from_request`                        |
| `build.flags` / `run.flags`   | no       | Each flag must match the language's `flag_allowlist` (`-std=*` globs)       |
| `build.limits` / `run.limits` | no       | `wall_time_s`, `memory_kb`, `max_processes`. Can only lower the defaults    |

Unknown fields and wrong JSON types are rejected (`invalid_json`).

### Response: always 200 when the job ran

```json
{
  "status": "wrong_output",
  "build": {"status": "ok", "stdout": "", "stderr": "", "duration_ms": 412},
  "tests": [
    {"status": "accepted", "stdout": "5\n", "stderr": "", "duration_ms": 6, "memory_peak_kb": 3120},
    {"status": "wrong_output", "stdout": "7\n", "stderr": "", "duration_ms": 5, "memory_peak_kb": 3088}
  ]
}
```

- `build` is present only for languages with a build step.
- `memory_peak_kb` comes from cgroup v2 and is `0` when cgroups are unavailable.
- Each of `stdout` and `stderr` is capped at 1 MiB and ends with
  `[output truncated]` when it was cut.

### Verdicts

| Status                       | Meaning                                                                   |
|------------------------------|---------------------------------------------------------------------------|
| `accepted`                   | Exit code 0 and stdout matches `expected_stdout` exactly                  |
| `output_whitespace_mismatch` | Matches only after trimming leading and trailing whitespace               |
| `wrong_output`               | Anything else (internal whitespace is not normalised)                     |
| `time_exceeded`              | Wall-time or CPU-time limit hit                                           |
| `memory_exceeded`            | OOM kill (cgroup) or the runtime reported an allocation failure           |
| `runtime_error`              | Non-zero exit or killed by a signal (including seccomp)                   |
| `build_failed`               | Top level only: compilation failed, and every test is `not_executed`      |
| `internal_error`             | The sandbox could not run the program                                     |

The top-level `status` is `accepted` only if every test is accepted. Otherwise
it is the first non-accepted test's status, or `build_failed`.

### Error responses

| HTTP | `error.code`          | When                                                       |
|------|-----------------------|------------------------------------------------------------|
| 400  | `invalid_json`        | Malformed JSON, unknown field or wrong type                |
| 400  | `body_too_large`      | Body larger than `max_body_bytes`                          |
| 400  | `source_too_large`    | `source` larger than `max_source_bytes`                    |
| 400  | `unknown_language`    | `language` is not configured                               |
| 400  | `invalid_filename`    | Separators, leading dot, control characters, too long, reserved |
| 400  | `disallowed_flag`     | A flag is not in the allowlist                             |
| 400  | `invalid_limits`      | A limit is negative or above the language maximum          |
| 400  | `invalid_test_count`  | Zero tests or more than `max_tests`                        |
| 401  | `unauthorized`        | Missing, malformed or wrong bearer token                   |
| 429  | `queue_full`          | No execution slot freed within `queue_timeout_s`           |
| 500  | `internal_error`      | The sandbox failed (counted in `/info` stats)              |
| 503  | `sandbox_unavailable` | nsjail is not installed and dev mode is off                |
