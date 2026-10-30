"""Suite registry: turns the check catalog into runnable cases.

A check runs only if it has both a catalog entry (``checks/<ID>.yaml``) and an
implementation, and applies to the server's transport. Checks are added
milestone by milestone (CONF in M1, FUZZ and ROB in M3, SEC in M4, EVAL in M5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcpprobe.engine.catalog import Suite, Transport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcpprobe.engine.catalog import Check
    from mcpprobe.engine.runner import Case

SUITE_ORDER = (Suite.CONF, Suite.FUZZ, Suite.ROB, Suite.SEC, Suite.EVAL)


@dataclass(frozen=True)
class SuiteOptions:
    allow_destructive: bool = False
    transport: Transport = Transport.STDIO


def _conf_cases(catalog: Sequence[Check], options: SuiteOptions) -> list[Case]:
    from mcpprobe.engine.runner import Case
    from mcpprobe.suites.conf.checks import CASES, TIMEOUT_FACTORS

    checks = {check.id: check for check in catalog}
    cases: list[Case] = []
    for check_id, suffix, run, transports in CASES:
        check = checks.get(check_id)
        if check is None or options.transport not in transports:
            continue
        if options.transport not in check.transports:
            continue
        cases.append(
            Case(
                id=f"{check_id}/{suffix}" if suffix else check_id,
                suite=Suite.CONF,
                severity=check.severity,
                run=run,
                title=check.title,
                era=check.era,
                spec_ref=check.spec_ref,
                timeout_factor=TIMEOUT_FACTORS.get(check_id, 1.0),
            )
        )
    return cases


def collect(
    suites: Sequence[Suite], catalog: Sequence[Check], options: SuiteOptions
) -> list[tuple[Suite, list[Case]]]:
    """Cases per requested suite, in the spec's fixed suite order."""
    plan: list[tuple[Suite, list[Case]]] = []
    for suite in SUITE_ORDER:
        if suite not in suites:
            continue
        cases = _conf_cases(catalog, options) if suite is Suite.CONF else []
        plan.append((suite, cases))
    return plan
