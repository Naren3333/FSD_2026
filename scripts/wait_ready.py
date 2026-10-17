"""Probe all ten applications from their private network, then the public Kong and Keycloak ports, with bounded
startup wait."""

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 180  # seconds for both phases together
SERVICES = [
    "identity",
    "curriculum",
    "content",
    "assessment",
    "grading",
    "analytics",
    "ai-core",
    "question-generation-agent",
    "student-performance-agent",
    "question-chatbot-agent",
]
# What the tests and the browser call from the host. Kong starts last and needs a few seconds to listen after its
# container starts, so the applications being ready says nothing about it.
PUBLIC = {
    "kong": ("http://127.0.0.1:8000/api/identity/classrooms", 401),
    "keycloak": ("http://127.0.0.1:8081/realms/learning", 200),
}
# Runs inside the analytics container, on the private network, and prints each service's readiness status.
PROBE = f"""
import json, urllib.request
result = {{}}
for name in {SERVICES!r}:
    try:
        with urllib.request.urlopen("http://" + name + ":8080/health/ready", timeout=3) as response:
            result[name] = response.status
    except Exception:
        result[name] = 503
print(json.dumps(result))
"""


def status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def applications_ready() -> bool:
    """Whether every application answers ready. A probe that hangs, or prints something other than its JSON
    (analytics still starting, say), counts as not ready yet, to be asked again."""
    command = ["docker", "compose", "exec", "-T", "analytics", "python", "-c", PROBE]
    try:
        result = subprocess.run(
            command, cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=40, check=False
        )
        statuses = json.loads(result.stdout) if result.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError):
        return False
    return (
        isinstance(statuses, dict)
        and len(statuses) == len(SERVICES)
        and all(value == 200 for value in statuses.values())
    )


def public_ports_ready() -> bool:
    return all(status(url) == expected for url, expected in PUBLIC.values())


def wait(ready: Callable[[], bool], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(2)
    return False


def main() -> None:
    deadline = time.monotonic() + TIMEOUT
    if not wait(applications_ready, deadline):
        raise SystemExit(f"Applications did not become ready within {TIMEOUT} seconds")
    print("PASS: all ten applications ready")
    if not wait(public_ports_ready, deadline):
        states = ", ".join(f"{name} {status(url)}" for name, (url, _) in PUBLIC.items())
        raise SystemExit(f"Kong or Keycloak did not answer within {TIMEOUT} seconds: {states}")
    print("PASS: Kong and Keycloak answer on their public ports")


if __name__ == "__main__":
    main()
