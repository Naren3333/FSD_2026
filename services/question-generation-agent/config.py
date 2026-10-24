import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

# below this top rerank score the chosen documents say too little about the request, and the agent answers
# insufficient_source without a model call.
RELEVANCE_FLOOR = 0.5
MAX_REVIEW_RETRIES = 2
REVIEW_OFF = ("off", "false", "0", "no")


@dataclass(frozen=True)
class Settings(AgentSettings):
    curriculum_url: str
    relevance_floor: float = RELEVANCE_FLOOR
    review: bool = True  # a reviewer pass over the drafts that passed the checks, with retries
    review_retries: int = MAX_REVIEW_RETRIES
    review_deadline: float = 240.0  # every round must finish within this many seconds of the request start


REQUIRED = {**AGENT_ENV, "curriculum_url": "CURRICULUM_URL"}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED),
        relevance_floor=float(os.environ.get("RELEVANCE_FLOOR", RELEVANCE_FLOOR)),
        review=os.environ.get("QUESTION_REVIEW", "on").strip().lower() not in REVIEW_OFF,
        review_retries=min(
            max(int(os.environ.get("QUESTION_REVIEW_RETRIES", MAX_REVIEW_RETRIES)), 0), MAX_REVIEW_RETRIES
        ),
        review_deadline=min(
            max(float(os.environ.get("QUESTION_REVIEW_DEADLINE", Settings.review_deadline)), 30.0), 240.0
        ),
    )
