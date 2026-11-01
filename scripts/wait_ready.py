"""probe all ten applications from their private network, then the public kong, keycloak and edge ports, with bounded startup wait."""

import argparse
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from local_stack import EDGE_PORT, KEYCLOAK_PORT, KONG_PORT, ROOT

TIMEOUT = 180  # seconds for both phases together, unless --timeout says otherwise
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
# what the tests and the browser call from the host, on the ports docker-compose.yml publishes for this
# stack: kong and keycloak directly, and the gateway and sign-in through the edge by host name.
PUBLIC = {
    "kong": (f"http://127.0.0.1:{KONG_PORT}/api/identity/classrooms", 401, None),
    "keycloak": (f"http://127.0.0.1:{KEYCLOAK_PORT}/realms/learning", 200, None),
    "edge": (f"https://127.0.0.1:{EDGE_PORT}/api/identity/classrooms", 401, "app.localhost"),
    "edge sign-in": (f"https://127.0.0.1:{EDGE_PORT}/realms/learning", 200, "auth.localhost"),
}
# the edge's certificate comes from its own local authority, which only the browser is asked to trust.
EDGE_TLS = ssl._create_unverified_context()
# runs inside the analytics container, on the private network, and prints each service's readiness status.
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


def status(url: str, host: str | None = None) -> int:
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=3, context=EDGE_TLS if host else None) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def applications_ready() -> bool:
    """whether every application answers ready."""
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
    return all(status(url, host) == expected for url, expected, host in PUBLIC.values())


def wait(ready: Callable[[], bool], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(2)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=TIMEOUT, help="seconds to wait for both phases together"
    )
    timeout = parser.parse_args().timeout
    deadline = time.monotonic() + timeout
    if not wait(applications_ready, deadline):
        raise SystemExit(f"Applications did not become ready within {timeout} seconds")
    print("PASS: all ten applications ready")
    if not wait(public_ports_ready, deadline):
        states = ", ".join(f"{name} {status(url, host)}" for name, (url, _, host) in PUBLIC.items())
        raise SystemExit(f"Kong, Keycloak or the edge did not answer within {timeout} seconds: {states}")
    print("PASS: Kong, Keycloak and the edge answer on their public ports")


if __name__ == "__main__":
    main()
