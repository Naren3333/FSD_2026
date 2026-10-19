import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

ANSWER_MAX_TOKENS = 800
# Below this top rerank score a turn is refused without a model call (fsd_test's golden-set floor).
RELEVANCE_FLOOR = 0.61
# Once a turn passed RELEVANCE_FLOOR, the search for one of its topics adds passages if its best one reaches this
# (question generation's floor): a narrow topic query scores lower than a whole question about it.
TOPIC_FLOOR = 0.5


@dataclass(frozen=True)
class Settings(AgentSettings):
    database_url: str
    answer_max_tokens: int = ANSWER_MAX_TOKENS
    relevance_floor: float = RELEVANCE_FLOOR
    topic_floor: float = TOPIC_FLOOR
    adaptive_rewrite: bool = True
    adaptive_search: bool = True
    context_tokens: int = 8000

    def __post_init__(self) -> None:
        if not 1000 <= self.context_tokens <= 60000:
            raise ValueError("CHAT_CONTEXT_TOKENS must be 1000–60000")


REQUIRED = {"database_url": "DATABASE_URL", **AGENT_ENV}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED),
        answer_max_tokens=int(os.environ.get("ANSWER_MAX_TOKENS", ANSWER_MAX_TOKENS)),
        relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR)),
        topic_floor=float(os.environ.get("TOPIC_FLOOR", TOPIC_FLOOR)),
        adaptive_rewrite=os.environ.get("ADAPTIVE_REWRITE", "true").lower() == "true",
        adaptive_search=os.environ.get("ADAPTIVE_SEARCH", "true").lower() == "true",
        context_tokens=int(os.environ.get("CHAT_CONTEXT_TOKENS", "8000")),
    )
