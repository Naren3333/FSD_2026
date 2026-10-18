import os
from dataclasses import dataclass

from learning_transport import require_env

# Below this top rerank score the chosen documents say too little about the request, and the agent answers
# INSUFFICIENT_SOURCE without a model call. Lower than the chatbot's 0.61, because a topic name is a vaguer query than
# a student's question; not yet calibrated on real classroom documents.
RELEVANCE_FLOOR = 0.3


@dataclass(frozen=True)
class Settings:
    identity_url: str
    curriculum_url: str
    content_url: str
    ai_core_url: str
    token_url: str  # Keycloak's token endpoint, for this agent's own service account (ai-core calls only)
    client_id: str
    client_secret: str
    relevance_floor: float = RELEVANCE_FLOOR


REQUIRED = {
    "identity_url": "IDENTITY_URL",
    "curriculum_url": "CURRICULUM_URL",
    "content_url": "CONTENT_URL",
    "ai_core_url": "AI_CORE_URL",
    "token_url": "AI_TOKEN_URL",
    "client_id": "AI_CLIENT_ID",
    "client_secret": "AI_CLIENT_SECRET",
}


def load() -> Settings:
    require_env(*REQUIRED.values())
    values = {field: os.environ[name].rstrip("/") for field, name in REQUIRED.items()}
    return Settings(**values, relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR)))
