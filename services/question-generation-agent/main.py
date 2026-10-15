"""Question generation (ADR 0002): not authorized yet, so every request that matches the contract answers 501."""

from learning_transport import placeholder_app

from contracts import RequestContract, ResponseContract

app = placeholder_app(
    "question-generation-agent",
    "/v1/question_generation",
    "question_generation",
    RequestContract,
    ResponseContract,
)
