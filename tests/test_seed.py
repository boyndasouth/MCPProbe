from __future__ import annotations

import pytest

from mcpprobe.engine.seed import SEED_BITS, derive_seed, new_seed, rng


def test_derive_seed_is_stable_across_runs() -> None:
    # Pinned value: if this changes, every saved report and replay case shifts.
    assert derive_seed(42, "fuzz", "search_issues") == derive_seed(42, "fuzz", "search_issues")
    assert derive_seed(42, "fuzz") == 226143005


def test_labels_give_independent_streams() -> None:
    assert derive_seed(42, "fuzz") != derive_seed(42, "rob")
    assert derive_seed(42, "a", "bc") != derive_seed(42, "ab", "c")


def test_rng_is_reproducible() -> None:
    first = [rng(7, "order").random() for _ in range(3)]
    second = [rng(7, "order").random() for _ in range(3)]
    assert first == second


@pytest.mark.parametrize("seed", [-1, 2**SEED_BITS])
def test_rejects_out_of_range_seed(seed: int) -> None:
    with pytest.raises(ValueError, match="seed"):
        derive_seed(seed)


def test_new_seed_in_range() -> None:
    assert 0 <= new_seed() < 2**SEED_BITS
