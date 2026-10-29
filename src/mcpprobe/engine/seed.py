"""One ``--seed`` controls all randomness.

Components never share a ``random.Random``; each derives its own stream from
the run seed plus a stable label. Adding a new consumer therefore never shifts
the random sequence another component sees, which keeps reports byte-identical
across versions when nothing relevant changed.
"""

from __future__ import annotations

import hashlib
import random
import secrets

SEED_BITS = 32


def new_seed() -> int:
    """A fresh seed for runs without ``--seed``; it is printed in the report."""
    return secrets.randbits(SEED_BITS)


def derive_seed(seed: int, *labels: str) -> int:
    """Stable sub-seed for ``labels`` (e.g. ``("fuzz", "search_issues")``)."""
    if not 0 <= seed < 2**SEED_BITS:
        raise ValueError(f"seed must be in [0, 2**{SEED_BITS})")
    material = "\x1f".join((str(seed), *labels)).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:4], "big")


def rng(seed: int, *labels: str) -> random.Random:
    """An independent RNG stream for one component."""
    return random.Random(derive_seed(seed, *labels))
