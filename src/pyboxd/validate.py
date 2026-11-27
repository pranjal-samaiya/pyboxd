"""Input validation: filenames and compiler/runtime flags."""

from __future__ import annotations

from collections.abc import Sequence

MAX_FILENAME_LEN = 128
# Names used as mount points inside the jail root.
RESERVED_NAMES = frozenset({"bin", "dev", "etc", "lib", "lib64", "proc", "sys", "tmp", "usr"})


class ValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_filename(name: str) -> None:
    """Reject anything that could escape or misbehave inside the jail directory.

    Rejected: empty names, > 128 chars, control characters (incl. NUL and DEL),
    path separators, and a leading dot (hidden files, ``.`` and ``..``).
    """
    if not name:
        raise ValidationError("invalid_filename", "filename cannot be empty")
    if len(name.encode("utf-8")) > MAX_FILENAME_LEN:
        raise ValidationError(
            "invalid_filename",
            f"filename exceeds {MAX_FILENAME_LEN} byte limit (got {len(name.encode())})",
        )
    for i, ch in enumerate(name):
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValidationError(
                "invalid_filename",
                f"filename contains invalid control character (0x{code:02x} at position {i})",
            )
        if ch in ("/", "\\"):
            raise ValidationError(
                "invalid_filename", "filename cannot contain path separators (/ or \\)"
            )
    if name.startswith("."):
        raise ValidationError(
            "invalid_filename",
            "filename cannot start with a dot (hidden files and traversal are not allowed)",
        )
    if name in RESERVED_NAMES:
        raise ValidationError("invalid_filename", f"filename {name!r} is reserved")


def _matches(flag: str, allowlist: Sequence[str]) -> bool:
    for entry in allowlist:
        if entry == flag:
            return True
        if entry.endswith("*") and flag.startswith(entry[:-1]):
            return True
    return False


def validate_flags(flags: Sequence[str], allowlist: Sequence[str]) -> None:
    """Every flag must match the allowlist exactly or via a trailing ``*`` glob.

    An empty allowlist means no flags are accepted at all.
    """
    if not flags:
        return
    if not allowlist:
        raise ValidationError(
            "disallowed_flag", f"no flags allowed, but received {len(flags)} flag(s)"
        )
    rejected = [f for f in flags if not _matches(f, allowlist)]
    if rejected:
        raise ValidationError("disallowed_flag", "flags not in allowlist: " + ", ".join(rejected))
