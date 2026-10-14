"""Run independent service quality gates and stop on the first failing command."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GO_MODULES = [
    "packages/platform",
    *(f"services/{s}" for s in ["identity", "curriculum", "content", "assessment", "grading"]),
]
PYTHON_SERVICES = [
    "analytics",
    "ai-core",
    "question-generation-agent",
    "student-performance-agent",
    "question-chatbot-agent",
]
WEB_SCRIPTS = ["format:check", "lint", "typecheck", "test", "build"]

if shutil.which("golangci-lint") is None:
    raise SystemExit(
        "golangci-lint is required: go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0"
    )
env = dict(
    os.environ,
    GOCACHE=str(ROOT / ".local/go-build"),
    GOMODCACHE=str(ROOT / ".local/go-mod"),
    PYTHONPATH=str(ROOT / "packages/python-platform"),
    MYPYPATH=str(ROOT / "packages/python-platform"),
)


def run(command: list[str], cwd: Path) -> None:
    print(f"[{cwd.relative_to(ROOT)}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def python(*args: str) -> list[str]:
    return [sys.executable, "-m", *args]


for name in GO_MODULES:
    cwd = ROOT / name
    files = [str(f) for f in cwd.glob("*.go")]
    result = subprocess.run(["gofmt", "-l", *files], env=env, capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise SystemExit("Go formatting required: " + result.stdout)
    run(["go", "vet", "./..."], cwd)
    run(["golangci-lint", "run", "--config", str(ROOT / ".golangci.yml"), "./..."], cwd)
    run(["go", "test", "./..."], cwd)
    run(["go", "build", "-o", os.devnull, "./..."], cwd)

platform = ROOT / "packages/python-platform"
for command in [
    python("ruff", "format", "--check", "."),
    python("ruff", "check", "."),
    python("pytest", "-q"),
]:
    run(command, platform)
for name in PYTHON_SERVICES:
    cwd = ROOT / "services" / name
    run(python("ruff", "format", "--check", "."), cwd)
    run(python("ruff", "check", "."), cwd)
    run(python("mypy", "main.py"), cwd)
    run(python("pytest", "-q"), cwd)
# The repository's own scripts: formatting and lint only, since most need the running stack.
run(python("ruff", "format", "--check", "--config", str(ROOT / "ruff.toml"), "scripts"), ROOT)
run(python("ruff", "check", "--config", str(ROOT / "ruff.toml"), "scripts"), ROOT)
for script in WEB_SCRIPTS:
    run(["npm.cmd" if os.name == "nt" else "npm", "run", script], ROOT / "apps/web")
print("PASS: all local quality gates")
