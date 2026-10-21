"""reuse exact embedding inputs, including document title/heading and embedding model identity."""

import asyncio
import math

from learning_transport.metrics import increment

import local_cache
import openai_client
from settings import get_settings


async def embed(texts: list[str], tally: openai_client.Tally) -> list[list[float]]:
    settings = get_settings()
    root = settings.cache_dir / "embeddings-v1"
    names = [local_cache.key(f"{settings.embedding_model}|1536|{text}".encode()) for text in texts]
    batch_key = local_cache.key("|".join(names).encode())
    async with local_cache.singleflight("embeddings:" + batch_key):
        saved: dict[str, list[float]] = {}
        for name in dict.fromkeys(names):
            value = await asyncio.to_thread(local_cache.read, root / f"{name}.json")
            if (
                isinstance(value, list)
                and len(value) == 1536
                and all(isinstance(v, int | float) and math.isfinite(v) for v in value)
            ):
                saved[name] = value
        missing = {name: text for name, text in zip(names, texts, strict=True) if name not in saved}
        increment("learning_embedding_cache_hits_total", len(saved))
        increment("learning_embedding_cache_misses_total", len(missing))
        todo = list(missing)
        for start in range(0, len(todo), openai_client.EMBED_BATCH):
            keys = todo[start : start + openai_client.EMBED_BATCH]
            vectors, _ = await openai_client.embed([missing[k] for k in keys], tally)
            for name, vector in zip(keys, vectors, strict=True):
                saved[name] = vector
                await asyncio.to_thread(local_cache.write, root / f"{name}.json", vector)
        return [saved[name] for name in names]
