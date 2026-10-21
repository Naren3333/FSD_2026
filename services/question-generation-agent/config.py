import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

# below this top rerank score the chosen documents say too little about the request, and the agent answers
# insufficient_source without a model call.
RELEVANCE_FLOOR = 0.5


@dataclass(frozen=True)
class Settings(AgentSettings):
    curriculum_url: str
    relevance_floor: float = RELEVANCE_FLOOR


REQUIRED = {**AGENT_ENV, "curriculum_url": "CURRICULUM_URL"}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED), relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR))
    )
