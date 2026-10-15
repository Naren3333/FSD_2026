import os
from dataclasses import dataclass

from learning_transport import require_env

ANSWER_MAX_TOKENS = 800
# Below this top rerank score a turn is refused without a model call (fsd_test's golden-set floor).
RELEVANCE_FLOOR = 0.61


@dataclass(frozen=True)
class Settings:
    database_url: str
    identity_url: str
    content_url: str
    ai_core_url: str
    token_url: str  # Keycloak's token endpoint, for this agent's own service account (ai-core calls only)
    client_id: str
    client_secret: str
    answer_max_tokens: int = ANSWER_MAX_TOKENS
    relevance_floor: float = RELEVANCE_FLOOR


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
    require_env(*REQUIRED.values())
    values = {field: os.environ[name].rstrip("/") for field, name in REQUIRED.items()}
    return Settings(
        **values,
        answer_max_tokens=int(os.environ.get("ANSWER_MAX_TOKENS", ANSWER_MAX_TOKENS)),
        relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR)),
    )
