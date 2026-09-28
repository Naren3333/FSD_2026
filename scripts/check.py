"""Run independent service quality gates and stop on the first failing command."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
env = dict(os.environ, GOCACHE=str(ROOT/".local/go-build"), GOMODCACHE=str(ROOT/".local/go-mod"), PYTHONPATH=str(ROOT/"packages/python-platform"), MYPYPATH=str(ROOT/"packages/python-platform"))


def run(command: list[str], cwd: Path) -> None:
    print(f"[{cwd.relative_to(ROOT)}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


for name in ["packages/platform", *[f"services/{s}" for s in ["identity", "curriculum", "content", "assessment", "grading"]]]:
    cwd = ROOT/name
    files = list(cwd.glob("*.go"))
    result = subprocess.run(["gofmt", "-l", *map(str, files)], env=env, capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise SystemExit("Go formatting required: " + result.stdout)
    for command in [["go", "vet", "./..."], ["go", "test", "./..."], ["go", "build", "./..."]]:
        run(command, cwd)
run([sys.executable,"-m","pytest","-q"], ROOT/"packages/python-platform")
for name in ["analytics", "ai-core", "question-generation-agent", "student-performance-agent", "question-chatbot-agent"]:
    cwd = ROOT/"services"/name
    for args in [["ruff", "format", "--check", "."], ["ruff", "check", "."], ["mypy", "main.py"], ["pytest", "-q"]]:
        run([sys.executable, "-m", *args], cwd)
for script in ["lint", "typecheck", "test", "build"]:
    run(["npm.cmd" if os.name == "nt" else "npm", "run", script], ROOT/"apps/web")
print("PASS: all local quality gates")
