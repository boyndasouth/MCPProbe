# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Project scaffold: uv project, src layout, ruff, mypy `--strict`, pytest.
- CLI skeleton (`run`, `replay`, `checks list`, `checks explain`) with the
  documented exit codes.
- Check catalog loader with schema validation (spec link required).
- Seed derivation so one `--seed` controls all randomness.
- CI matrix: Linux, macOS, Windows × Python 3.11–3.13.
