# Technology Sources

These are the primary documentation sources for the decisions recorded in the
[technology and Codex integration section of the consolidated
specification](../docs/spec.md#3-technology-and-codex-integration).

Reviewed: 2026-08-19

## Python

- [Python 3.11 `asyncio` coroutines and tasks](https://docs.python.org/3.11/library/asyncio-task.html)
  - Source coverage: `TaskGroup`, task cancellation, `asyncio.timeout`, and monotonic deadlines.

## Packaging and dependency management

- [Python Packaging User Guide: writing `pyproject.toml`](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
  - Source coverage: project metadata, build-system declarations, dependencies, and entry points.
- [Python Packaging User Guide: `src` layout versus flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
  - Source coverage: import-path behavior and package layout tradeoffs.
- [uv project guide](https://docs.astral.sh/uv/guides/projects/)
  - Source coverage: project environments, `pyproject.toml`, and the cross-platform lockfile.
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)
  - Source coverage: locked and frozen installs, project synchronization, and editable installs.
- [Hatch build configuration](https://hatch.pypa.io/latest/config/build/)
  - Source coverage: Hatchling build-system declaration, package selection, and reproducible builds.

## Validation

- [Pydantic strict mode](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/)
  - Source coverage: strict validation at model, field, and validation-call boundaries.
- [Pydantic model configuration](https://pydantic.dev/docs/validation/latest/api/pydantic/config/)
  - Source coverage: `ConfigDict`, strict behavior, and rejection of unknown fields.

## Tests and quality

- [pytest good integration practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
  - Source coverage: `src` layout, installed-package testing, and `importlib` import mode.
- [pytest-asyncio documentation](https://pytest-asyncio.readthedocs.io/en/stable/)
  - Source coverage: pytest support for asyncio code.
- [Ruff linter](https://docs.astral.sh/ruff/linter/)
  - Source coverage: Python linting and fix behavior.
- [Ruff formatter](https://docs.astral.sh/ruff/formatter/)
  - Source coverage: formatting, check mode, and formatter/linter interaction.
- [mypy getting started](https://mypy.readthedocs.io/en/stable/getting_started.html)
  - Source coverage: static type checking and strict-mode configuration.
