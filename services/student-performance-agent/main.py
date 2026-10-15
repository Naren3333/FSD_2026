"""Student performance interpretation (ADR 0002): not authorized yet, so every request that matches the contract
answers 501."""

from learning_transport import placeholder_app

from contracts import RequestContract, ResponseContract

app = placeholder_app(
    "student-performance-agent",
    "/v1/performance_interpretation",
    "performance_interpretation",
    RequestContract,
    ResponseContract,
)
