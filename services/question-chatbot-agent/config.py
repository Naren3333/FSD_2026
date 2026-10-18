import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

ANSWER_MAX_TOKENS = 800
# Below this top rerank score a turn is refused without a model call (fsd_test's golden-set floor).
RELEVANCE_FLOOR = 0.61


@dataclass(frozen=True)
class Settings(AgentSettings):
    database_url: str
    answer_max_tokens: int = ANSWER_MAX_TOKENS
    relevance_floor: float = RELEVANCE_FLOOR


REQUIRED = {"database_url": "DATABASE_URL", **AGENT_ENV}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED),
        answer_max_tokens=int(os.environ.get("ANSWER_MAX_TOKENS", ANSWER_MAX_TOKENS)),
        relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR)),
    )
