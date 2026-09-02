"""Package version lookup kept separate to avoid public-API import cycles."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("recursive-codex")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"
