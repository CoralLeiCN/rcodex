"""Deterministic, content-free manifest construction and integrity validation."""

from __future__ import annotations

import codecs
import fnmatch
import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from rcodex.config import RunConfig
from rcodex.models import ContextManifest, FinalPayload, ManifestEntry

DEFAULT_EXCLUDED_DIRECTORIES = {
    ".git",
    ".rcodex",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

MEDIA_TYPES = {
    ".bash": "text/x-shellscript",
    ".c": "text/x-c",
    ".cc": "text/x-c++",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".cpp": "text/x-c++",
    ".cs": "text/x-csharp",
    ".css": "text/css",
    ".csv": "text/csv",
    ".fish": "text/x-shellscript",
    ".go": "text/x-go",
    ".h": "text/x-c",
    ".hpp": "text/x-c++",
    ".htm": "text/html",
    ".html": "text/html",
    ".ini": "text/plain",
    ".java": "text/x-java",
    ".js": "text/javascript",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".jsx": "text/jsx",
    ".kt": "text/x-kotlin",
    ".kts": "text/x-kotlin",
    ".less": "text/css",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".php": "text/x-php",
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".r": "text/x-r",
    ".rb": "text/x-ruby",
    ".rs": "text/x-rust",
    ".sass": "text/x-sass",
    ".scala": "text/x-scala",
    ".scss": "text/x-scss",
    ".sh": "text/x-shellscript",
    ".sql": "application/sql",
    ".svg": "image/svg+xml",
    ".swift": "text/x-swift",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".tsv": "text/tab-separated-values",
    ".tsx": "text/tsx",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".zsh": "text/x-shellscript",
}

SUPPORTED_NAMES = {
    "Dockerfile": "text/plain",
    "LICENSE": "text/plain",
    "Makefile": "text/x-makefile",
}


class ManifestError(ValueError):
    """The context cannot be represented by a stable manifest."""


class ManifestDeadlineError(ManifestError):
    """The controller deadline expired while scanning context."""


class ManifestCancelledError(ManifestError):
    """The caller cancelled a context scan running in a worker thread."""


class EvidenceValidationError(ValueError):
    """Model-authored evidence does not resolve against the manifest."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def manifest_sha256(manifest: ContextManifest) -> str:
    payload = manifest.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _matches(relative_path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in patterns)


def _media_type(path: Path) -> str | None:
    return SUPPORTED_NAMES.get(path.name, MEDIA_TYPES.get(path.suffix.lower()))


def _check_scan_limits(deadline: float | None, cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ManifestCancelledError("context scan cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ManifestDeadlineError("run deadline expired while scanning context")


def _candidate_paths(
    root: Path,
    config: RunConfig,
    deadline: float | None,
    cancel_event: Event | None,
) -> list[tuple[str, Path, str]]:
    candidates: list[tuple[str, Path, str]] = []

    def traversal_error(error: OSError) -> None:
        raise ManifestError(f"cannot traverse context: {type(error).__name__}") from error

    for directory, directory_names, file_names in os.walk(
        root, topdown=True, onerror=traversal_error, followlinks=False
    ):
        _check_scan_limits(deadline, cancel_event)
        current = Path(directory)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            child = current / name
            relative = child.relative_to(root).as_posix()
            if child.is_symlink() or name in DEFAULT_EXCLUDED_DIRECTORIES:
                continue
            if _matches(relative, config.exclude) or _matches(f"{relative}/", config.exclude):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            _check_scan_limits(deadline, cancel_event)
            path = current / name
            relative = path.relative_to(root).as_posix()
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                continue
            media_type = _media_type(path)
            if media_type is None or path.is_symlink():
                continue
            if config.include and not _matches(relative, config.include):
                continue
            if _matches(relative, config.exclude):
                continue
            candidates.append((relative, path, media_type))
            if len(candidates) > config.max_manifest_entries:
                raise ManifestError("context exceeds max_manifest_entries candidate-path budget")
    candidates.sort(key=lambda item: item[0])
    return candidates


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    relative_path: str
    bytes: int
    sha256: str
    media_type: str
    line_count: int


@dataclass(frozen=True, slots=True)
class _StreamedText:
    bytes: int
    sha256: str
    line_count: int
    valid_text: bool = True


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mode == after.st_mode
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _stream_text(
    path: Path,
    remaining_bytes: int,
    deadline: float | None,
    cancel_event: Event | None,
) -> _StreamedText:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    observed_bytes = 0
    newline_count = 0
    has_text = False
    ends_with_newline = False
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            _check_scan_limits(deadline, cancel_event)
            observed_bytes += len(chunk)
            if observed_bytes > remaining_bytes:
                raise ManifestError("context exceeds max_context_bytes")
            if b"\x00" in chunk:
                return _StreamedText(observed_bytes, "", 0, valid_text=False)
            try:
                text = decoder.decode(chunk)
            except UnicodeDecodeError:
                return _StreamedText(observed_bytes, "", 0, valid_text=False)
            digest.update(chunk)
            if text:
                has_text = True
                newline_count += text.count("\n")
                ends_with_newline = text.endswith("\n")
    try:
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return _StreamedText(observed_bytes, "", 0, valid_text=False)
    if tail:
        has_text = True
        newline_count += tail.count("\n")
        ends_with_newline = tail.endswith("\n")
    return _StreamedText(
        bytes=observed_bytes,
        sha256=digest.hexdigest(),
        line_count=newline_count + (1 if has_text and not ends_with_newline else 0),
    )


def _scan_file(
    relative: str,
    path: Path,
    media_type: str,
    remaining_bytes: int,
    deadline: float | None,
    cancel_event: Event | None,
) -> tuple[_ScannedFile | None, int]:
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            return None, 0
        streamed = _stream_text(path, remaining_bytes, deadline, cancel_event)
        after = path.stat(follow_symlinks=False)
    except PermissionError:
        return None, 0
    except FileNotFoundError as exc:
        raise ManifestError(f"context file changed while scanning: {relative}") from exc
    except OSError:
        return None, 0

    if not _same_file(before, after) or (streamed.valid_text and streamed.bytes != after.st_size):
        raise ManifestError(f"context file changed while hashing: {relative}")
    if not streamed.valid_text:
        return None, streamed.bytes
    return (
        _ScannedFile(
            relative_path=relative,
            bytes=streamed.bytes,
            sha256=streamed.sha256,
            media_type=media_type,
            line_count=streamed.line_count,
        ),
        streamed.bytes,
    )


def build_manifest(
    context_root: Path,
    config: RunConfig,
    *,
    deadline: float | None = None,
    cancel_event: Event | None = None,
) -> ContextManifest:
    _check_scan_limits(deadline, cancel_event)
    root = context_root.resolve(strict=True)
    if not root.is_dir():
        raise ManifestError("context must be a directory")

    entries: list[ManifestEntry] = []
    total_bytes = 0
    for relative, path, media_type in _candidate_paths(root, config, deadline, cancel_event):
        _check_scan_limits(deadline, cancel_event)
        scanned, observed_bytes = _scan_file(
            relative,
            path,
            media_type,
            config.max_context_bytes - total_bytes,
            deadline,
            cancel_event,
        )
        total_bytes += observed_bytes
        if total_bytes > config.max_context_bytes:
            raise ManifestError("context exceeds max_context_bytes")
        if scanned is None:
            continue

        if len(entries) + 1 > config.max_manifest_entries:
            raise ManifestError("context exceeds max_manifest_entries")
        entries.append(
            ManifestEntry(
                id=f"file_{len(entries) + 1:06d}",
                relative_path=scanned.relative_path,
                bytes=scanned.bytes,
                sha256=scanned.sha256,
                media_type=scanned.media_type,
                line_count=scanned.line_count,
            )
        )

    if not entries:
        raise ManifestError("context contains no supported UTF-8 text files")
    return ContextManifest(context_root=str(root), entries=entries)


def integrity_changes(before: ContextManifest, after: ContextManifest) -> list[str]:
    old = {entry.relative_path: entry for entry in before.entries}
    new = {entry.relative_path: entry for entry in after.entries}
    changes: list[str] = []
    for path in sorted(old.keys() | new.keys()):
        if path not in old:
            changes.append(f"added:{path}")
        elif path not in new:
            changes.append(f"removed:{path}")
        elif old[path] != new[path]:
            changes.append(f"changed:{path}")
    return changes


def validate_evidence(payload: FinalPayload, manifest: ContextManifest) -> None:
    entries = {entry.id: entry for entry in manifest.entries}
    for evidence in payload.evidence:
        entry = entries.get(evidence.context_entry_id)
        if entry is None:
            raise EvidenceValidationError(f"unknown context entry: {evidence.context_entry_id}")
        if evidence.path != entry.relative_path:
            raise EvidenceValidationError(
                f"evidence path does not match {evidence.context_entry_id}"
            )
        if evidence.line_end > entry.line_count:
            raise EvidenceValidationError(
                f"evidence line range exceeds {evidence.context_entry_id}"
            )
