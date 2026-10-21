"""server-sent events: writing frames, and reading them from a streamed response."""

import json
from collections.abc import AsyncIterator
from typing import Any


def sse_event(name: str, data: dict[str, Any]) -> str:
    """one sse frame: `event: <name>` and the data as compact json."""
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def read_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """(event name, json data) for each frame in a stream of lines, such as httpx's `aiter_lines()`."""
    event = ""
    async for line in lines:
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            yield event, json.loads(line[6:])
