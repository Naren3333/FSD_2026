import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, EFFORTS, AgentSettings, read_settings

# below this top rerank score the chosen documents say too little about the request, and the agent answers
# INSUFFICIENT_SOURCE without a model call. calibrated on the eval corpus (2026-10-19, rerank-3); the scores and
# trade-offs are in docs/decisions.md (decision 0006), so recheck them there before changing it.
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
    # the question and guidance reviewers' reasoning effort; unset, the check profile's own (adr 0026)
    review_effort: str | None = None


REQUIRED = {**AGENT_ENV, "curriculum_url": "CURRICULUM_URL"}


def review_effort() -> str | None:
    """QUESTION_REVIEW_EFFORT: none, low or medium, or unset for the check profile's own; anything else is refused at start."""
    value = os.environ.get("QUESTION_REVIEW_EFFORT", "").strip().lower()
    if value and value not in EFFORTS:
        raise ValueError(f"QUESTION_REVIEW_EFFORT must be one of {', '.join(EFFORTS)}, or unset")
    return value or None


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
        review_effort=review_effort(),
    )
