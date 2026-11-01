"""The docs site's check catalog is generated from checks/ and must not drift."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]


def _generator() -> Any:
    path = ROOT / "scripts" / "gen_check_docs.py"
    spec = importlib.util.spec_from_file_location("gen_check_docs", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_check_pages_match_the_catalog() -> None:
    # If this fails: uv run python scripts/gen_check_docs.py, then commit docs/checks/.
    assert _generator().main(["--check"]) == 0


def test_every_page_in_the_nav_exists() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    for entry in config["nav"]:
        (page,) = entry.values()
        assert (ROOT / "docs" / page).is_file(), page
