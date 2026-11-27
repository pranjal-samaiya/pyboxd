"""nsjail command-line construction.

Two argument sets are used:

* **build** — chroot is the jail directory, mounted read-write so the compiler
  can write its artifact, with a private tmpfs ``/tmp``.
* **run** — chroot is the same jail directory, mounted read-only.

In both phases only ``/usr``, ``/lib``, ``/lib64``, ``/bin`` and ``/etc`` are
bind-mounted (read-only), so a submission can never read the host filesystem or
another request's files. Paths inside the jail are relative to the jail root
(``/solution.py``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import ResourceLimits

DEFAULT_NSJAIL_PATH = "/usr/sbin/nsjail"
JAIL_PATH_ENV = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Kafel seccomp policy: kill the process on syscalls that a judged program never
# legitimately needs and that are useful for escapes or host interference.
SECCOMP_POLICY = """POLICY pyboxd_safe {
    KILL_PROCESS {
        ptrace, process_vm_readv, process_vm_writev,
        init_module, finit_module, delete_module, kexec_load,
        reboot, settimeofday, adjtimex, clock_adjtime,
        mknodat, chroot, pivot_root, unshare, setns,
        userfaultfd, name_to_handle_at, open_by_handle_at,
        acct, bpf, syslog, add_key, request_key, keyctl,
        fanotify_init, capset, mount
    }
}
USE pyboxd_safe DEFAULT ALLOW"""


def sandbox_uid() -> str:
    """UID/GID the jailed program runs as (default: nobody). Never root."""
    return os.environ.get("PYBOXD_SANDBOX_UID", "65534")


def nsjail_path() -> str:
    return os.environ.get("NSJAIL_PATH") or DEFAULT_NSJAIL_PATH


def nsjail_available() -> bool:
    path = nsjail_path()
    if Path(path).is_file():
        return os.access(path, os.X_OK)
    return shutil.which(path) is not None


def _common_args(
    limits: ResourceLimits, rlimit_as_mb: int, log_fd: int, cgroup_name: str | None
) -> list[str]:
    memory_mb = max(limits.memory_kb // 1024, 1)
    if rlimit_as_mb > 0:
        memory_mb = rlimit_as_mb
    # fmt: off
    args = [
        "--mode", "o",
        "--time_limit", str(limits.wall_time_s),
        "--rlimit_as", str(memory_mb),
        "--rlimit_cpu", str(limits.wall_time_s),
        "--rlimit_nproc", str(limits.max_processes),
        "--rlimit_nofile", "1024",
        "--rlimit_core", "0",
        "--rlimit_stack", "8",
        "--rlimit_fsize", "100",
        "--max_cpus", "1",
        "--log_fd", str(log_fd),
        # inside:outside:count - map to the unprivileged host id, not host root
        "--user", f"{sandbox_uid()}:{sandbox_uid()}:1",
        "--group", f"{sandbox_uid()}:{sandbox_uid()}:1",
        "--bindmount_ro", "/usr:/usr",
        "--bindmount_ro", "/lib:/lib",
        "--bindmount_ro", "/bin:/bin",
    ]
    # fmt: on
    if Path("/lib64").exists():
        args += ["--bindmount_ro", "/lib64:/lib64"]
    if cgroup_name:
        # nsjail creates its per-run cgroup inside our parent cgroup, whose
        # hierarchical memory.peak / memory.events the runner then reads.
        args += [
            "--use_cgroupv2",
            "--cgroupv2_mount", f"/sys/fs/cgroup/{cgroup_name}",
            "--cgroup_mem_swap_max", "0",
        ]  # fmt: skip
    return args


def build_args(
    jail_dir: Path,
    limits: ResourceLimits,
    cmd: str,
    cmd_args: list[str],
    *,
    rlimit_as_mb: int = 0,
    log_fd: int = 3,
    cgroup_name: str | None = None,
) -> list[str]:
    """nsjail arguments for the BUILD phase.

    The jail directory is the (writable) root, so the compiler can write its
    artifact but cannot read anything else on the host, including other
    requests' jail directories. ``/tmp`` is a private tmpfs.
    """
    args = _common_args(limits, rlimit_as_mb, log_fd, cgroup_name)
    # fmt: off
    args += [
        "--bindmount_ro", "/etc:/etc",
        "--tmpfsmount", "/tmp",  # noqa: S108 (private tmpfs inside the jail)
        "--env", "HOME=/tmp",
        "--env", "TMPDIR=/tmp",
        "--env", "GOCACHE=/tmp/gocache",
        "--env", JAIL_PATH_ENV,
        "--seccomp_string", SECCOMP_POLICY,
        "--rw",
        "--chroot", str(jail_dir),
        "--cwd", "/",
        "--",
        cmd,
        *cmd_args,
    ]
    # fmt: on
    return args


def run_args(
    jail_dir: Path,
    limits: ResourceLimits,
    cmd: str,
    cmd_args: list[str],
    *,
    rlimit_as_mb: int = 0,
    extra_bindmounts_ro: tuple[str, ...] = (),
    log_fd: int = 3,
    cgroup_name: str | None = None,
) -> list[str]:
    """nsjail arguments for the RUN phase (chroot=jail dir)."""
    args = _common_args(limits, rlimit_as_mb, log_fd, cgroup_name)
    args += ["--bindmount_ro", "/etc:/etc"]
    for mount in extra_bindmounts_ro:
        args += ["--bindmount_ro", mount]
    # fmt: off
    args += [
        "--env", "HOME=/",
        "--env", "TMP=/tmp",
        "--env", "TMPDIR=/tmp",
        "--env", JAIL_PATH_ENV,
        "--seccomp_string", SECCOMP_POLICY,
        "--chroot", str(jail_dir),
        "--cwd", "/",
        "--",
        cmd,
        *cmd_args,
    ]
    # fmt: on
    return args
