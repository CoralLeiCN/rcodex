"""Bounded, manifest-only file access for the isolated REPL's context proxy."""

from __future__ import annotations

import codecs
import os
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from typing import Any, Literal

from pydantic import Field, ValidationError

from rcodex.context import canonical_json_bytes
from rcodex.models import ContextManifest, ManifestEntry, StrictModel

CONTEXT_READ_BYTES = 64 * 1024
CONTEXT_PAGE_BYTES = 64 * 1024
# A read's JSON can expand sixfold through escaping; paths and metadata add overhead.
CONTEXT_MESSAGE_BYTES = 512 * 1024


class ContextAccessError(ValueError):
    """A context request is invalid or its file is no longer safely readable."""


class _FilesRequest(StrictModel):
    operation: Literal["files"]
    offset: int = Field(ge=0)


class _ReadRequest(StrictModel):
    operation: Literal["read"]
    entry_id: str = Field(pattern=r"^file_[0-9]{6}$")
    offset: int = Field(ge=0)
    max_bytes: int = Field(ge=4, le=CONTEXT_READ_BYTES)


class ContextReader:
    """Read bounded portions of a run's manifest without caching file bodies."""

    def __init__(self, manifest: ContextManifest) -> None:
        self._root = Path(manifest.context_root)
        self._entries = manifest.entries
        self._by_id = {entry.id: entry for entry in manifest.entries}

    @staticmethod
    def _check_active(deadline: float, cancel_event: Event) -> None:
        if cancel_event.is_set() or time.monotonic() >= deadline:
            raise ContextAccessError("context request deadline expired or request cancelled")

    def request(
        self, request: dict[str, Any], *, deadline: float, cancel_event: Event
    ) -> dict[str, Any]:
        self._check_active(deadline, cancel_event)
        try:
            if request.get("operation") == "files":
                page = _FilesRequest.model_validate(request, strict=True)
                return self._files(page.offset)
            read = _ReadRequest.model_validate(request, strict=True)
        except ValidationError as exc:
            raise ContextAccessError("invalid context request arguments") from exc
        entry = self._by_id.get(read.entry_id)
        if entry is None:
            raise ContextAccessError("unknown manifest entry ID")
        if read.offset > entry.bytes:
            raise ContextAccessError("offset exceeds the manifest file size")
        return self._read(entry, read.offset, read.max_bytes, deadline, cancel_event)

    def _files(self, offset: int) -> dict[str, Any]:
        if offset > len(self._entries):
            raise ContextAccessError("offset exceeds the manifest entry count")
        entries: list[dict[str, Any]] = []
        size = 2  # JSON list brackets.
        for entry in self._entries[offset : offset + 100]:
            value = entry.model_dump(mode="json")
            encoded_size = len(canonical_json_bytes(value)) + (1 if entries else 0)
            if size + encoded_size > CONTEXT_PAGE_BYTES:
                break
            entries.append(value)
            size += encoded_size
        next_offset = offset + len(entries)
        return {
            "entries": entries,
            "next_offset": next_offset,
            "eof": next_offset == len(self._entries),
        }

    def _read(
        self,
        entry: ManifestEntry,
        offset: int,
        max_bytes: int,
        deadline: float,
        cancel_event: Event,
    ) -> dict[str, Any]:
        # Walk via directory descriptors: a replaced symlink below the context root must not
        # redirect the controller to another directory. O_NONBLOCK also prevents FIFO hangs.
        parts = Path(entry.relative_path).parts
        if not parts or Path(entry.relative_path).is_absolute() or ".." in parts:
            raise ContextAccessError("invalid manifest path")
        try:
            with ExitStack() as stack:
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                directory = os.open(self._root, directory_flags)
                try:
                    for part in parts[:-1]:
                        self._check_active(deadline, cancel_event)
                        child = os.open(part, directory_flags, dir_fd=directory)
                        os.close(directory)
                        directory = child
                    descriptor = os.open(
                        parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                finally:
                    os.close(directory)
                stack.callback(os.close, descriptor)
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size != entry.bytes:
                    raise ContextAccessError(
                        "context file type or size changed since manifest scan"
                    )
                self._check_active(deadline, cancel_event)
                os.lseek(descriptor, offset, os.SEEK_SET)
                raw = os.read(descriptor, min(max_bytes, entry.bytes - offset))
                self._check_active(deadline, cancel_event)
                after = os.fstat(descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ContextAccessError("context file changed while reading")
        except OSError as exc:
            raise ContextAccessError("manifest file is unavailable or contains a symlink") from exc
        if len(raw) != min(max_bytes, entry.bytes - offset):
            raise ContextAccessError("context file returned an incomplete read")
        if b"\x00" in raw:
            raise ContextAccessError("context file is no longer supported UTF-8 text")
        try:
            # Keep a trailing partial code point for the next request. An offset inside a
            # code point fails decoding, rather than silently dropping bytes.
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            text = decoder.decode(raw, final=offset + len(raw) == entry.bytes)
            pending, _ = decoder.getstate()
        except UnicodeDecodeError as exc:
            raise ContextAccessError(
                "offset is not a UTF-8 boundary or file contains invalid UTF-8"
            ) from exc
        next_offset = offset + len(raw) - len(pending)
        return {
            "context_entry_id": entry.id,
            "path": entry.relative_path,
            "offset": offset,
            "next_offset": next_offset,
            "eof": next_offset == entry.bytes,
            "text": text,
        }
