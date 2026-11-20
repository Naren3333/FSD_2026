"""bounded operational json cache."""

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

TTL = 7 * 86400
MAX_BYTES = 256 * 1024 * 1024
log = logging.getLogger("ai-core.cache")
_locks: dict[tuple[asyncio.AbstractEventLoop, str], tuple[asyncio.Lock, int]] = {}
_maintenance = threading.Lock()
_writes = 0
_last_prune = 0.0


def key(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@asynccontextmanager
async def singleflight(name: str) -> AsyncIterator[None]:
    k = (asyncio.get_running_loop(), name)
    lock, users = _locks.get(k, (asyncio.Lock(), 0))
    _locks[k] = lock, users + 1
    try:
        async with lock:
            yield
    finally:
        _, users = _locks[k]
        if users == 1:
            del _locks[k]
        else:
            _locks[k] = lock, users - 1


def read(path: Path) -> Any:
    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > TTL or stat.st_size > MAX_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(path: Path, value: Any) -> None:
    """cache/storage failures never fail successful provider work."""
    write_many([(path, value)])


def write_many(entries: list[tuple[Path, Any]]) -> None:
    """write each entry, then prune at most once for the batch."""
    global _writes, _last_prune
    written = [path.parent for path, value in entries if _put(path, value)]
    if not written:
        return
    with _maintenance:
        before, _writes = _writes, _writes + len(written)
        prune = before // 64 != _writes // 64 or time.monotonic() - _last_prune > 60
        if prune:
            _last_prune = time.monotonic()
    if not prune:
        return
    try:
        for folder in set(written):
            _prune(folder)
    except OSError:
        log.warning("cache_write_failed")


def _put(path: Path, value: Any) -> bool:
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        if tmp.stat().st_size > MAX_BYTES:
            return False
        os.replace(tmp, path)
        return True
    except OSError:
        log.warning("cache_write_failed")
        return False
    finally:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def _prune(folder: Path) -> None:
    entries = sorted(
        ((entry, entry.stat()) for entry in folder.glob("*.json")),
        key=lambda e: e[1].st_mtime,
        reverse=True,
    )
    size = 0
    now = time.time()
    for entry, stat in entries:
        size += stat.st_size
        if size > MAX_BYTES or now - stat.st_mtime > TTL:
            entry.unlink(missing_ok=True)
