"""Shared transport, authentication and operational helpers for the Python services. No business models.

Database pools live in `learning_transport.db`, imported only by services that have psycopg."""

from learning_transport.app import OIDC_SETTINGS, application, placeholder_app, require_env
from learning_transport.auth import AI_CLIENT, STUDENT, TEACHER, Claims, actor, caller, keyset, requires
from learning_transport.errors import (
    Contract,
    ErrorBody,
    ErrorResponse,
    UpstreamError,
    failure,
    unavailable,
    upstream_error,
)
from learning_transport.identity import RELATION_STUDENT, RELATION_TEACHER, IdentityClient, require_relation
from learning_transport.sse import read_events, sse_event
from learning_transport.tokens import ServiceToken

__all__ = [
    "AI_CLIENT",
    "OIDC_SETTINGS",
    "RELATION_STUDENT",
    "RELATION_TEACHER",
    "STUDENT",
    "TEACHER",
    "Claims",
    "Contract",
    "ErrorBody",
    "ErrorResponse",
    "IdentityClient",
    "ServiceToken",
    "UpstreamError",
    "actor",
    "application",
    "caller",
    "failure",
    "keyset",
    "placeholder_app",
    "read_events",
    "require_env",
    "require_relation",
    "requires",
    "sse_event",
    "unavailable",
    "upstream_error",
]
