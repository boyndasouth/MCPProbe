"""Loads and validates the check catalog (one YAML file per check in ``checks/``).

The catalog is data, not code: adding a check means adding a YAML file, a
failing fixture server, a passing fixture server and a test. The engine only
needs to know the shape of an entry, which is defined here.
"""

from __future__ import annotations

import re
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CHECK_ID_PATTERN = r"^(CONF|FUZZ|ROB|SEC|EVAL)-\d{3}$"
SPEC_HOST = "modelcontextprotocol.io"


class Severity(StrEnum):
    ERROR = "error"  # violates a MUST; fails the run
    WARN = "warn"  # violates a SHOULD or uses a deprecated feature
    INFO = "info"  # advice


class Era(StrEnum):
    MODERN = "modern"  # stateless 2026-07-28
    LEGACY = "legacy"  # initialize-era revisions
    BOTH = "both"  # applies to either era
    DUAL = "dual"  # only meaningful for servers that serve both eras


class Transport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class Suite(StrEnum):
    CONF = "CONF"
    FUZZ = "FUZZ"
    ROB = "ROB"
    SEC = "SEC"
    EVAL = "EVAL"


class Check(BaseModel):
    """One entry in the check catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=CHECK_ID_PATTERN)]
    title: Annotated[str, Field(min_length=1)]
    severity: Severity
    era: Era
    spec_ref: str
    """URL of the spec section this check enforces, including its anchor."""
    transports: tuple[Transport, ...] = (Transport.STDIO, Transport.HTTP)
    description: Annotated[str, Field(min_length=1)]

    @property
    def suite(self) -> Suite:
        return Suite(self.id.split("-", 1)[0])

    @field_validator("spec_ref")
    @classmethod
    def _spec_ref_points_at_spec(cls, value: str) -> str:
        # Every check must cite the section it enforces; no MUST/SHOULD, no check.
        if not re.match(rf"^https://(www\.)?{re.escape(SPEC_HOST)}/", value):
            raise ValueError(f"spec_ref must be a https://{SPEC_HOST}/ URL")
        if "#" not in value:
            raise ValueError("spec_ref must link to a section anchor (#...)")
        return value

    @field_validator("transports")
    @classmethod
    def _transports_non_empty_and_sorted(
        cls, value: tuple[Transport, ...]
    ) -> tuple[Transport, ...]:
        if not value:
            raise ValueError("transports must not be empty")
        # Normalised order keeps reports byte-identical regardless of YAML order.
        return tuple(sorted(set(value)))


class CatalogError(Exception):
    """Raised when the catalog cannot be loaded. Maps to exit code 3."""


def default_checks_dir() -> Path:
    """Locate the catalog: packaged copy in a wheel, ``checks/`` in a checkout."""
    packaged = resources.files("mcpprobe").joinpath("_checks")
    if packaged.is_dir():
        return Path(str(packaged))
    # Editable install / source tree: <repo>/src/mcpprobe/engine/catalog.py
    return Path(__file__).resolve().parents[3] / "checks"


def load_check(path: Path) -> Check:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"{path.name}: expected a mapping at the top level")
    try:
        check = Check.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    if path.stem != check.id:
        raise CatalogError(f"{path.name}: file name must match id {check.id!r}")
    return check


def load_catalog(directory: Path | None = None) -> tuple[Check, ...]:
    """Load every ``*.yaml`` check, sorted by id.

    File names must equal check ids, so ids are unique by construction.
    """
    directory = directory if directory is not None else default_checks_dir()
    if not directory.is_dir():
        raise CatalogError(f"checks directory not found: {directory}")
    checks = (load_check(path) for path in directory.glob("*.yaml"))
    return tuple(sorted(checks, key=lambda check: check.id))
