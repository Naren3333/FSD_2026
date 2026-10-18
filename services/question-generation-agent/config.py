import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

# Below this top rerank score the chosen documents say too little about the request, and the agent answers
# INSUFFICIENT_SOURCE without a model call. Lower than the chatbot's 0.61, because a topic name is a vaguer query than
# a student's question; not yet calibrated on real classroom documents.
RELEVANCE_FLOOR = 0.3


@dataclass(frozen=True)
class Settings(AgentSettings):
    curriculum_url: str
    relevance_floor: float = RELEVANCE_FLOOR


REQUIRED = {**AGENT_ENV, "curriculum_url": "CURRICULUM_URL"}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED), relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR))
    )
