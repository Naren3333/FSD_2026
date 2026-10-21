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
    global _writes, _last_prune
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        if tmp.stat().st_size > MAX_BYTES:
            return
        os.replace(tmp, path)
        with _maintenance:
            _writes += 1
            prune = _writes % 64 == 0 or time.monotonic() - _last_prune > 60
            if prune:
                _last_prune = time.monotonic()
        if not prune:
            return
        entries = sorted(path.parent.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        size = 0
        now = time.time()
        for entry in entries:
            stat = entry.stat()
            size += stat.st_size
            if size > MAX_BYTES or now - stat.st_mtime > TTL:
                entry.unlink(missing_ok=True)
    except OSError:
        log.warning("cache_write_failed")
    finally:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
