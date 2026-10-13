"""Probe all ten applications from their private network, then the public Kong and Keycloak ports, with bounded startup wait."""
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics", "ai-core", "question-generation-agent", "student-performance-agent", "question-chatbot-agent"]
# What the tests and the browser call from the host. Kong starts last and needs a few seconds to listen after its
# container starts, so the applications being ready says nothing about it.
PUBLIC = {"kong": ("http://127.0.0.1:8000/api/identity/classrooms", 401), "keycloak": ("http://127.0.0.1:8081/realms/learning", 200)}
probe = """
import json,urllib.request
services = %r
result = {}
for name in services:
    try:
        with urllib.request.urlopen('http://'+name+':8080/health/ready', timeout=3) as response:
            result[name] = response.status
    except Exception:
        result[name] = 503
print(json.dumps(result))
""" % SERVICES


def status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    result = subprocess.run(["docker", "compose", "exec", "-T", "analytics", "python", "-c", probe], cwd=ROOT, text=True, capture_output=True, timeout=40)
    if result.returncode == 0:
        states = json.loads(result.stdout)
        if all(value == 200 for value in states.values()):
            print("PASS: all ten applications ready")
            break
    time.sleep(2)
else:
    raise SystemExit("Applications did not become ready within 180 seconds")
while time.monotonic() < deadline:
    if all(status(url) == expected for url, expected in PUBLIC.values()):
        print("PASS: Kong and Keycloak answer on their public ports")
        break
    time.sleep(2)
else:
    raise SystemExit("Kong or Keycloak did not answer within 180 seconds: " + ", ".join(f"{name} {status(url)}" for name, (url, _) in PUBLIC.items()))
