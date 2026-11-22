"""runs inside the chatbot's container: checks one probe batch from stdin with the deployed support check (adr 0025)."""

import asyncio
import json
import sys

import config
import httpx
import support
from clients import Services


async def main() -> None:
    job = json.load(sys.stdin)
    batch = job["batch"]
    async with httpx.AsyncClient() as http:
        try:
            found = await support.check(
                Services(config.load(), http),
                batch["passages"],
                [(s["text"], s["cites"]) for s in batch["statements"]],
                job["correlation"],
                {**job["billed_to"], "role": "teacher"},
            )
        except Exception as e:  # as the deployed check: a failure is recorded, not raised
            print(json.dumps({"status": "failed", "error": getattr(e, "code", None) or type(e).__name__}))
            return
    verdicts = {s["id"]: v for s, v in zip(batch["statements"], found, strict=True)}
    print(json.dumps({"status": "checked", "verdicts": verdicts}))


asyncio.run(main())
