from __future__ import annotations

from pathlib import Path

import pytest

from mcpprobe.engine.catalog import (
    CatalogError,
    Check,
    Era,
    Severity,
    Suite,
    Transport,
    default_checks_dir,
    load_catalog,
)

DATA = Path(__file__).parent / "data" / "checks"


def _check(**overrides: object) -> Check:
    fields: dict[str, object] = {
        "id": "CONF-999",
        "title": "t",
        "severity": "error",
        "era": "modern",
        "spec_ref": "https://modelcontextprotocol.io/specification/2026-07-28/basic#x",
        "description": "d",
    }
    fields.update(overrides)
    return Check.model_validate(fields)


def test_loads_valid_catalog_sorted_by_id() -> None:
    catalog = load_catalog(DATA / "valid")
    assert [c.id for c in catalog] == ["CONF-901", "SEC-901"]
    conf, sec = catalog
    assert conf.severity is Severity.ERROR
    assert conf.era is Era.MODERN
    assert conf.suite is Suite.CONF
    assert sec.suite is Suite.SEC
    assert sec.transports == (Transport.HTTP,)


def test_transports_are_normalised() -> None:
    # YAML lists [http, stdio]; order must not depend on how the author wrote it.
    conf = load_catalog(DATA / "valid")[0]
    assert conf.transports == (Transport.HTTP, Transport.STDIO)


def test_rejects_spec_ref_outside_spec_site() -> None:
    with pytest.raises(CatalogError, match="spec_ref"):
        load_catalog(DATA / "invalid")


def test_rejects_file_name_that_differs_from_id() -> None:
    with pytest.raises(CatalogError, match="file name must match"):
        load_catalog(DATA / "mismatch")


def test_missing_directory_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope")


@pytest.mark.parametrize(
    ("content", "message"),
    [("id: [unclosed", "invalid YAML"), ("- a list", "mapping")],
)
def test_rejects_malformed_files(tmp_path: Path, content: str, message: str) -> None:
    (tmp_path / "CONF-001.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(CatalogError, match=message):
        load_catalog(tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "CONF-1"},
        {"id": "XYZ-001"},
        {"severity": "fatal"},
        {"era": "ancient"},
        {"spec_ref": "https://modelcontextprotocol.io/specification/2026-07-28/basic"},
        {"transports": []},
        {"unexpected": "field"},
    ],
)
def test_check_model_rejects_bad_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic raises a ValueError subclass
        _check(**overrides)


def test_real_catalog_loads() -> None:
    # The shipped catalog must always be valid, even while it is empty.
    assert default_checks_dir().is_dir()
    for check in load_catalog():
        assert check.spec_ref.startswith("https://modelcontextprotocol.io/")
