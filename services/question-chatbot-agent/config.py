import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    identity_url: str
    content_url: str
    ai_core_url: str
    token_url: str  # Keycloak's token endpoint, for this agent's own service account (ai-core calls only)
    client_id: str
    client_secret: str
    answer_max_tokens: int = 800
    # Below this top rerank score a turn is refused without a model call (fsd_test's golden-set floor).
    relevance_floor: float = 0.61


REQUIRED = {
    "database_url": "DATABASE_URL",
    "identity_url": "IDENTITY_URL",
    "content_url": "CONTENT_URL",
    "ai_core_url": "AI_CORE_URL",
    "token_url": "AI_TOKEN_URL",
    "client_id": "AI_CLIENT_ID",
    "client_secret": "AI_CLIENT_SECRET",
}


def load() -> Settings:
    missing = [name for name in REQUIRED.values() if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    values = {field: os.environ[name].rstrip("/") for field, name in REQUIRED.items()}
    return Settings(
        **values,
        answer_max_tokens=int(os.environ.get("ANSWER_MAX_TOKENS", "800")),
        relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", "0.61")),
    )
