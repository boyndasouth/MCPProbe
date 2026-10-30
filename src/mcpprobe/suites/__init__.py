"""Suite registry: turns the check catalog into runnable cases.

Each suite module exposes ``collect(catalog, options) -> list[Case]``. Until a
suite is implemented it contributes no cases; checks are added milestone by
milestone (CONF in M1, FUZZ and ROB in M3, SEC in M4, EVAL in M5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcpprobe.engine.catalog import Check, Suite
    from mcpprobe.engine.runner import Case


@dataclass(frozen=True)
class SuiteOptions:
    allow_destructive: bool = False


def collect(
    suites: Sequence[Suite], catalog: Sequence[Check], options: SuiteOptions
) -> list[tuple[Suite, list[Case]]]:
    """Cases per requested suite, in the spec's fixed suite order."""
    from mcpprobe.engine.catalog import Suite

    order = [Suite.CONF, Suite.FUZZ, Suite.ROB, Suite.SEC, Suite.EVAL]
    return [(suite, []) for suite in order if suite in suites]
