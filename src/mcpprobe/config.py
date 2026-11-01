"""``mcpprobe.toml``: optional configuration, in the shape the spec shows.

    [server]
    stdio = "python -m my_server"            # or: http = "http://127.0.0.1:8000/mcp"
    env = { API_KEY = "${API_KEY}" }          # stdio only
    headers = { Authorization = "Bearer ${TOKEN}" }   # http only

    [run]
    suites = ["conf", "fuzz", "rob", "sec"]
    seed = 42
    fuzz_examples_per_tool = 50
    timeout_ms = 10000
    allow_destructive = false

    [checks]
    disable = ["CONF-012"]
    severity_overrides = { "SEC-002" = "info" }

    [eval]            # read in M5; accepted and ignored until then

Command-line options win over the file, and the file wins over defaults.
``${NAME}`` is replaced by the environment variable ``NAME`` in server
settings, so secrets never have to be written into the file. Unknown keys and
unknown check ids are errors (exit code 3), so a typo never silently does
nothing.
"""

from __future__ import annotations

import os
import re
import tomllib
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcpprobe.engine.catalog import Severity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mcpprobe.engine.catalog import Check

DEFAULT_FILE = "mcpprobe.toml"
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid. Maps to exit code 3."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_Section):
    stdio: str | None = None
    http: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class RunConfig(_Section):
    suites: list[str] | None = None
    seed: Annotated[int, Field(ge=0, lt=2**32)] | None = None
    fuzz_examples_per_tool: Annotated[int, Field(ge=1, le=10_000)] | None = None
    timeout_ms: Annotated[int, Field(ge=100)] | None = None
    startup_timeout_ms: Annotated[int, Field(ge=100)] | None = None
    total_timeout_ms: Annotated[int, Field(ge=0)] | None = None
    concurrency: Annotated[int, Field(ge=2, le=256)] | None = None
    allow_destructive: bool | None = None
    allow_open_world: bool | None = None
    keep_going: bool | None = None
    cases_dir: str | None = None
    baseline: str | None = None


class ChecksConfig(_Section):
    disable: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    checks: ChecksConfig = Field(default_factory=ChecksConfig)
    eval: dict[str, Any] = Field(default_factory=dict)
    """M5 (tool-usability eval). Accepted now so files written for v1.1 still load."""
    source: str | None = None
    """The file this came from, for messages; None when there is no file."""


def _expand(value: str, where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"{where}: environment variable {name} is not set")
        return os.environ[name]

    return _VARIABLE.sub(replace, value)


def _expand_server(server: ServerConfig) -> ServerConfig:
    return ServerConfig(
        stdio=None if server.stdio is None else _expand(server.stdio, "[server] stdio"),
        http=None if server.http is None else _expand(server.http, "[server] http"),
        env={k: _expand(v, f"[server.env] {k}") for k, v in server.env.items()},
        headers={k: _expand(v, f"[server.headers] {k}") for k, v in server.headers.items()},
    )


def load_config(path: Path | None, *, cwd: Path) -> Config:
    """``path`` if given (it must exist), else ``mcpprobe.toml`` in ``cwd`` if present."""
    explicit = path is not None
    target = path if path is not None else cwd / DEFAULT_FILE
    if not target.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {target}")
        return Config()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{target}: {exc}") from exc
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise ConfigError(f"{target}: {problems}") from exc
    if config.server.stdio is not None and config.server.http is not None:
        raise ConfigError(f"{target}: [server] takes stdio or http, not both")
    return config.model_copy(
        update={"server": _expand_server(config.server), "source": str(target)}
    )


def apply_checks(catalog: Sequence[Check], checks: ChecksConfig, source: str) -> tuple[Check, ...]:
    """The catalog with ``disable`` removed and ``severity_overrides`` applied."""
    known = {check.id for check in catalog}
    for check_id in [*checks.disable, *checks.severity_overrides]:
        if check_id not in known:
            raise ConfigError(f"{source}: [checks] names unknown check {check_id!r}")
    disabled = set(checks.disable)
    out: list[Check] = []
    for check in catalog:
        if check.id in disabled:
            continue
        severity = checks.severity_overrides.get(check.id)
        out.append(check if severity is None else check.model_copy(update={"severity": severity}))
    return tuple(out)
