"""runs inside the question-generation-agent container (run_generation.py --probe pipes it in): reviews one probe batch read from stdin with the deployed reviewer prompt and client, billed through ai-core, and prints the verdicts as json."""

import asyncio
import json
import sys
import uuid

import config
import httpx
import pipeline
import prompts
import review
from clients import Services

from contracts import DraftQuestion, Excerpt, Generated

NAMESPACE = uuid.UUID("7f0c2a8e-5d1b-4c3e-9a6f-2b8d4e1c0a95")  # stable fake ids for the probe's passages


def fake(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


async def main() -> None:
    job = json.load(sys.stdin)
    batch = job["batch"]
    passages = [
        prompts.Passage(n, fake(key), fake("document"), "Probe notes", 1, text)
        for n, (key, text) in enumerate(batch["passages"].items(), start=1)
    ]
    by_key = {key: p for key, p in zip(batch["passages"], passages, strict=True)}
    drafts = [
        Generated(
            question=DraftQuestion(
                prompt=d["prompt"],
                type=d["type"],
                topic_id=fake("topic"),
                points=1 if d["type"] == "multiple_choice" else 2,
                options=d.get("options") or [],
                answer=d.get("answer") or "",
                accepted_answers=d.get("accepted") or [],
                sources=[
                    Excerpt(
                        document_id=by_key[s].document_id,
                        chunk_id=by_key[s].chunk_id,
                        page=1,
                        text=by_key[s].text,
                    )
                    for s in d["sources"]
                ],
            ),
            evidence="",
            explanation="",
        )
        for d in batch["drafts"]
    ]
    async with httpx.AsyncClient() as http:
        services = Services(config.load(), http)
        output = await services.generate(
            prompts.review_messages(job["topic"], job["difficulty"], drafts, passages, batch["existing"]),
            pipeline.REVIEW_TOKENS,
            prompts.review_schema(),
            "questions.reviewed",
            job["correlation"],
            {**job["billed_to"], "role": "teacher"},
        )
    verdicts = review.verdicts(output, len(drafts))
    print(json.dumps([{"passed": v.passed, "reason": v.reason, "note": v.note} for v in verdicts]))


asyncio.run(main())
