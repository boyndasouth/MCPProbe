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
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mcpprobe.engine.catalog import Check
    from mcpprobe.engine.runner import Case, CaseContext, Outcome

SUITE_ORDER = (Suite.CONF, Suite.FUZZ, Suite.ROB, Suite.SEC, Suite.EVAL)


@dataclass(frozen=True)
class SuiteOptions:
    allow_destructive: bool = False
    transport: Transport = Transport.STDIO


def _conf_cases(catalog: Sequence[Check], options: SuiteOptions) -> list[Case]:
    from mcpprobe.suites.conf.checks import CASES, FRESH_SESSIONS, TIMEOUT_FACTORS

    return _cases(Suite.CONF, CASES, TIMEOUT_FACTORS, FRESH_SESSIONS, catalog, options)


def _rob_cases(catalog: Sequence[Check], options: SuiteOptions) -> list[Case]:
    from mcpprobe.suites.rob.checks import CASES, TIMEOUT_FACTORS

    # Robustness does not depend on the era: run each case once per server.
    return _cases(Suite.ROB, CASES, TIMEOUT_FACTORS, {}, catalog, options, once=True)


def _sec_cases(catalog: Sequence[Check], options: SuiteOptions) -> list[Case]:
    from mcpprobe.suites.sec.checks import CASES, FRESH_SESSIONS, RELATED, TIMEOUT_FACTORS

    return _cases(
        Suite.SEC,
        CASES,
        TIMEOUT_FACTORS,
        FRESH_SESSIONS,
        catalog,
        options,
        once=True,
        related=RELATED,
    )


def _cases(
    suite: Suite,
    specs: Sequence[
        tuple[str, str, Callable[[CaseContext], Awaitable[Outcome]], frozenset[Transport]]
    ],
    timeout_factors: Mapping[str, float],
    fresh_sessions: Mapping[str, int],
    catalog: Sequence[Check],
    options: SuiteOptions,
    *,
    once: bool = False,
    related: Mapping[str, tuple[str, ...]] | None = None,
) -> list[Case]:
    from mcpprobe.engine.runner import Case, RelatedCheck

    checks = {check.id: check for check in catalog}
    cases: list[Case] = []
    for check_id, suffix, run, transports in specs:
        check = checks.get(check_id)
        if check is None or options.transport not in transports:
            continue
        if options.transport not in check.transports:
            continue
        cases.append(
            Case(
                id=f"{check_id}/{suffix}" if suffix else check_id,
                suite=suite,
                severity=check.severity,
                run=run,
                title=check.title,
                era=check.era,
                spec_ref=check.spec_ref,
                timeout_factor=timeout_factors.get(check_id, 1.0),
                fresh_sessions=fresh_sessions.get(check_id, 0),
                once=once,
                related=tuple(
                    RelatedCheck(rel.id, rel.severity, rel.spec_ref)
                    for rel in (checks.get(r) for r in (related or {}).get(check_id, ()))
                    if rel is not None
                ),
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


_BUILDERS = {
    Suite.CONF: _conf_cases,
    Suite.FUZZ: _fuzz_cases,
    Suite.ROB: _rob_cases,
    Suite.SEC: _sec_cases,
}


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
