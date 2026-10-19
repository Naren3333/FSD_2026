import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

# Below this top rerank score the chosen documents say too little about the request, and the agent answers
# INSUFFICIENT_SOURCE without a model call. Lower than the chatbot's 0.61, because a topic name is a vaguer query than
# a student's question. Calibrated on the eval corpus (2026-10-19, rerank-3): topics their documents cover scored
# 0.73-0.94 and teachers' questions 0.50-0.94; unrelated subjects scored 0.27-0.36 and topics the documents don't
# cover mostly 0.32-0.51.
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
