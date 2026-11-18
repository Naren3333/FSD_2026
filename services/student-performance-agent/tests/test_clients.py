"""topic metadata is fetched in bounded authenticated batches."""

import httpx
import pytest
from conftest import SETTINGS

from clients import Services


@pytest.mark.anyio
async def test_topic_names_batch_distinct_ids_and_keep_archived_names():
    ids = [f"topic-{n}" for n in range(121)]
    batches = []

    def handle(request):
        import json

        assert request.headers["authorization"] == "Bearer teacher"
        assert request.headers["x-correlation-id"] == "trace"
        assert request.url.path == "/v1/topics/batch"
        batch = json.loads(request.content)["ids"]
        batches.append(batch)
        return httpx.Response(200, json=[{"id": topic, "name": topic} for topic in batch[:-1]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        names = await Services(SETTINGS, http).topic_names("Bearer teacher", ids + ids[:5], "trace")
    assert list(map(len, batches)) == [50, 50, 21]
    assert len(names) == 118
