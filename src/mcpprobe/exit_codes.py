"""Process exit codes. These are a public contract: never renumber them."""

from enum import IntEnum


class ExitCode(IntEnum):
    PASSED = 0
    """Every check passed (warnings and info findings allowed)."""

    FAILED = 1
    """At least one finding with severity ``error``."""

    PROBE_ERROR = 2
    """The probe could not run: server did not start, was unreachable, etc."""

    CONFIG_INVALID = 3
    """``mcpprobe.toml``, a check file, or the command line was invalid."""
