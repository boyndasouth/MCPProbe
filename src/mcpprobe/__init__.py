"""MCPProbe: conformance, fuzzing and usability checks for MCP servers."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "mcpprobe-kit"
"""The name on PyPI (``pip install mcpprobe-kit``); the command is ``mcpprobe``."""

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
