"""Probe all ten applications from their private network with bounded startup wait."""
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics", "ai-core", "question-generation-agent", "student-performance-agent", "question-chatbot-agent"]
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
