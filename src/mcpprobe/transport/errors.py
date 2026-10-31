"""Transport-level failures. All of them mean "the probe could not run" (exit 2)."""


class TransportError(Exception):
    """Base class for wire-level failures."""


class ServerStartError(TransportError):
    """The server process could not be started."""


class TransportTimeout(TransportError):
    """Nothing arrived within the allowed time."""

    def __init__(self, message: str, *, progress: int = 0) -> None:
        super().__init__(message)
        self.progress = progress
        """Progress notifications seen for the request before it timed out."""


class TransportClosed(TransportError):
    """The server closed its output, exited, or the connection dropped."""


class LineTooLong(TransportError):
    """A stdout line exceeded the size cap without a newline."""
