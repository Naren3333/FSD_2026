import os
from dataclasses import dataclass

from learning_transport.agents import AGENT_ENV, AgentSettings, read_settings

# model turns per interpretation: tool turns, then one that must report.
MAX_TURNS = 5
# after this many seconds the next turn must report, so the teacher is not kept waiting.
DEADLINE = 120.0


@dataclass(frozen=True)
class Settings(AgentSettings):
    analytics_url: str
    grading_url: str
    assessment_url: str
    curriculum_url: str
    max_turns: int = MAX_TURNS
    deadline: float = DEADLINE


# this agent never searches content, so it needs no content url (adr 0014).
REQUIRED = {
    **{field: name for field, name in AGENT_ENV.items() if field != "content_url"},
    "analytics_url": "ANALYTICS_URL",
    "grading_url": "GRADING_URL",
    "assessment_url": "ASSESSMENT_URL",
    "curriculum_url": "CURRICULUM_URL",
}


def load() -> Settings:
    return Settings(
        **read_settings(REQUIRED),
        content_url="",
        max_turns=int(os.environ.get("PERFORMANCE_MAX_TURNS", MAX_TURNS)),
        deadline=float(os.environ.get("PERFORMANCE_DEADLINE", DEADLINE)),
    )
