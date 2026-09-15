"""Atomic artifacts and an append-only recursive trajectory log."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rcodex.context import canonical_json_bytes
from rcodex.models import (
    ArtifactPaths,
    ContextManifest,
    ManifestEntry,
    RunEvent,
    SessionRecord,
    StrictModel,
)

MAX_EVENT_BYTES = 16_384


class StorageError(RuntimeError):
    """A controller-owned artifact could not be persisted."""


def ensure_private_directory(path: Path) -> None:
    """Create a controller state directory and enforce owner-only access on POSIX."""

    try:
        existed = path.exists()
        missing: list[Path] = []
        cursor = path
        while not cursor.exists() and cursor != cursor.parent:
            missing.append(cursor)
            cursor = cursor.parent
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            for created in missing:
                created.chmod(0o700)
            mode = stat.S_IMODE(path.stat().st_mode)
            if existed and mode & 0o077:
                raise StorageError(
                    "existing controller state directory must deny group and other access"
                )
            if not existed:
                path.chmod(0o700)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("could not create private artifact directory") from exc


def atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    preserve_existing_mode: bool = False,
    secure_parent: bool = True,
) -> None:
    destination = path.resolve(strict=False)
    existing_mode: int | None = None
    if preserve_existing_mode:
        try:
            existing = destination.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError(f"could not inspect artifact: {destination.name}") from exc
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise StorageError(f"artifact target is not a regular file: {destination.name}")
            existing_mode = stat.S_IMODE(existing.st_mode)
    if secure_parent:
        ensure_private_directory(destination.parent)
    else:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"could not create output directory: {destination.parent.name}"
            ) from exc
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        mode = existing_mode if existing_mode is not None else 0o600
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            if os.name != "nt":
                os.chmod(temporary, mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise StorageError(f"could not write artifact: {destination.name}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: StrictModel, *, pretty: bool = False) -> None:
    payload = value.model_dump(mode="json")
    if pretty:
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    else:
        content = canonical_json_bytes(payload)
    atomic_write_bytes(path, content + b"\n")


def read_session(path: Path) -> SessionRecord:
    try:
        raw = path.read_bytes()
        return SessionRecord.model_validate_json(raw, strict=True)
    except (OSError, ValueError) as exc:
        raise StorageError("could not read persistent session") from exc


class RunStore:
    def __init__(self, state_directory: Path, run_id: str) -> None:
        ensure_private_directory(state_directory)
        runs_directory = state_directory / "runs"
        ensure_private_directory(runs_directory)
        self.run_directory = runs_directory / run_id
        try:
            self.run_directory.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            raise StorageError("could not create run directory") from exc
        self.request_path = self.run_directory / "request.json"
        self.manifest_path = self.run_directory / "context-manifest.json"
        self.nodes_directory = self.run_directory / "nodes"
        self.iterations_directory = self.run_directory / "iterations"
        self.result_path = self.run_directory / "result.json"
        self.events_path = self.run_directory / "events.jsonl"
        self._sequence = 0

    def artifacts(self, *, manifest: bool, nodes: bool, iterations: bool) -> ArtifactPaths:
        return ArtifactPaths(
            run_directory=str(self.run_directory),
            request=str(self.request_path),
            manifest=str(self.manifest_path) if manifest else None,
            nodes_directory=str(self.nodes_directory) if nodes else None,
            iterations_directory=str(self.iterations_directory) if iterations else None,
            result=str(self.result_path),
            events=str(self.events_path),
        )

    def write(self, path: Path, value: StrictModel) -> None:
        atomic_write_json(path, value)

    def write_child_context(self, node_id: str, text: str) -> tuple[ContextManifest, Path]:
        """Persist supplied text outside prompts, with a content-free node-local manifest."""
        root = self.run_directory / "contexts" / node_id
        manifest_path = root.with_suffix(".json")
        content = text.encode("utf-8")
        manifest = ContextManifest(
            context_root=str(root),
            entries=[
                ManifestEntry(
                    id="file_000001",
                    relative_path="input.txt",
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    media_type="text/plain",
                    line_count=text.count("\n") + int(bool(text) and not text.endswith("\n")),
                )
            ],
        )
        atomic_write_bytes(root / "input.txt", content)
        atomic_write_json(manifest_path, manifest)
        return manifest, manifest_path

    def node_path(self, node_id: str) -> Path:
        ensure_private_directory(self.nodes_directory)
        return self.nodes_directory / f"{node_id}.json"

    def iteration_path(self, node_id: str, iteration: int) -> Path:
        directory = self.iterations_directory / node_id
        ensure_private_directory(self.iterations_directory)
        ensure_private_directory(directory)
        return directory / f"{iteration:03d}.json"

    def event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        iteration: int | None = None,
        call_id: str | None = None,
    ) -> None:
        self._sequence += 1
        event = RunEvent(
            sequence=self._sequence,
            event_id=f"evt_{uuid.uuid4().hex}",
            timestamp=datetime.now(UTC),
            run_id=run_id,
            node_id=node_id,
            parent_node_id=parent_node_id,
            iteration=iteration,
            call_id=call_id,
            type=event_type,
            payload=payload or {},
        )
        content = canonical_json_bytes(event.model_dump(mode="json"))
        if len(content) > MAX_EVENT_BYTES:
            raise StorageError("event exceeds the 16384-byte limit")
        try:
            ensure_private_directory(self.events_path.parent)
            descriptor = os.open(
                self.events_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "ab") as handle:
                if os.name != "nt":
                    os.chmod(self.events_path, 0o600)
                handle.write(content + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError("could not append run event") from exc


def session_path(state_directory: Path, session_id: str) -> Path:
    return state_directory / "sessions" / f"{session_id}.json"
