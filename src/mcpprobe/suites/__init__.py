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
    from mcpprobe.suites.conf.checks import CASES, FRESH_SESSIONS, TIMEOUT_FACTORS

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
                fresh_sessions=FRESH_SESSIONS.get(check_id, 0),
            )
        )
    return cases


def _fuzz_cases(catalog: Sequence[Check], options: SuiteOptions) -> list[Case]:
    from mcpprobe.engine.runner import Case, RelatedCheck
    from mcpprobe.suites.fuzz.checks import (
        FRESH_SESSIONS,
        PRIMARY,
        RELATED,
        TIMEOUT_FACTOR,
        fuzz_tool,
    )

    checks = {check.id: check for check in catalog}
    primary = checks.get(PRIMARY)
    if primary is None or options.transport not in primary.transports:
        return []
    related = tuple(
        RelatedCheck(check.id, check.severity, check.spec_ref)
        for check in (checks.get(check_id) for check_id in RELATED)
        if check is not None
    )
    return [
        Case(
            id=PRIMARY,
            suite=Suite.FUZZ,
            severity=primary.severity,
            run=fuzz_tool,
            title=primary.title,
            era=primary.era,
            spec_ref=primary.spec_ref,
            timeout_factor=TIMEOUT_FACTOR,
            fresh_sessions=FRESH_SESSIONS,
            per_tool=True,
            once=True,
            related=related,
        )
    ]


_BUILDERS = {Suite.CONF: _conf_cases, Suite.FUZZ: _fuzz_cases}


def collect(
    suites: Sequence[Suite], catalog: Sequence[Check], options: SuiteOptions
) -> list[tuple[Suite, list[Case]]]:
    """Cases per requested suite, in the spec's fixed suite order."""
    plan: list[tuple[Suite, list[Case]]] = []
    for suite in SUITE_ORDER:
        if suite not in suites:
            continue
        builder = _BUILDERS.get(suite)
        plan.append((suite, builder(catalog, options) if builder else []))
    return plan
