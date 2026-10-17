"""Run the local quality gates in parallel: formatting, lint, types, unit tests and builds, plus a check that the
committed API contracts match the code.

Each Go module, Python service and the web app is a lane of commands that run in order, side by side. By default
the first failure stops the rest early (lanes not started are skipped, running ones stop after their current
command); --keep-going runs everything and reports every failing lane. Use --serial on a slow machine, or
--jobs N to cap how many lanes run at once. Tests that skip themselves, such as the database-backed ones that
scripts/db_tests.py runs against the stack, are listed at the end as not run.
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

env = dict(
    os.environ,
    GOCACHE=str(ROOT / ".local/go-build"),
    GOMODCACHE=str(ROOT / ".local/go-mod"),
    PYTHONPATH=str(ROOT / "packages/python-platform"),
    MYPYPATH=str(ROOT / "packages/python-platform"),
    # Angular's first build otherwise waits for an answer to its analytics question.
    NG_CLI_ANALYTICS="false",
    CI="1",
)
failed = threading.Event()
keep_going = False
skipped: list[str] = []  # tests that skipped themselves, with their reasons
print_lock = threading.Lock()


def python(*args: str) -> list[str]:
    return [sys.executable, "-m", *args]


def go_lane(name: str) -> list[tuple[list[str], Path]]:
    cwd = ROOT / name
    return [
        (["gofmt", "-l", "."], cwd),
        (["go", "vet", "./..."], cwd),
        # Without --allow-parallel-runners a second golangci-lint exits at once while another one runs.
        (
            [
                "golangci-lint",
                "run",
                "--allow-parallel-runners",
                "--config",
                str(ROOT / ".golangci.yml"),
                "./...",
            ],
            cwd,
        ),
        # -v only so skipped tests can be listed; the output is shown only when the lane fails.
        (["go", "test", "-v", "./..."], cwd),
        (["go", "build", "-o", os.devnull, "./..."], cwd),
    ]


def python_lane(cwd: Path, typed: bool) -> list[tuple[list[str], Path]]:
    steps = [(python("ruff", "format", "--check", "."), cwd), (python("ruff", "check", "."), cwd)]
    if typed:
        steps.append((python("mypy", "main.py"), cwd))
    return [*steps, (python("pytest", "-q", "-rs"), cwd)]


def lanes() -> dict[str, list[tuple[list[str], Path]]]:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    # The slowest lanes come first so they start before the pool fills.
    result = {"apps/web": [([npm, "run", script], ROOT / "apps/web") for script in WEB_SCRIPTS]}
    result.update({name: go_lane(name) for name in GO_MODULES})
    result["packages/python-platform"] = python_lane(ROOT / "packages/python-platform", typed=False)
    result.update({f"services/{n}": python_lane(ROOT / "services" / n, typed=True) for n in PYTHON_SERVICES})
    # The repository's own scripts and the Kong config generator: formatting, lint, and the tests that need no
    # stack (most scripts need the running stack, so they are not run here).
    config = str(ROOT / "ruff.toml")
    result["scripts"] = [
        (python("ruff", "format", "--check", "--config", config, "scripts", "infra"), ROOT),
        (python("ruff", "check", "--config", config, "scripts", "infra"), ROOT),
        (python("pytest", "-q", "-rs", "-p", "no:cacheprovider", "scripts/tests"), ROOT),
    ]
    # The contracts in contracts/ are generated from the code; fail if any differs from what the code produces.
    result["contracts"] = [([sys.executable, "scripts/export_contracts.py", "--check"], ROOT)]
    return result


def execute(command: list[str], cwd: Path, timeout: int) -> tuple[str, int]:
    """Runs a command with no input and returns its output and exit code; a stuck command is stopped.

    Output goes to a file rather than a pipe, so a child process left behind cannot keep the read open.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        # Its own process group, so stopping it also stops the processes it started (npm -> node -> workers).
        # No input: a command that asks a question fails instead of waiting for an answer.
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=os.name != "nt",
        )
        try:
            code = process.wait(timeout)
            stuck = ""
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code, stuck = 1, f"\nStopped: still running after {timeout}s."
        out.seek(0)
        return (out.read() + stuck).strip(), code


# A skipped Go test (go test -v prints the reason just before the result line), or pytest's -rs summary line.
SKIP = re.compile(r"^\s+\S+\.go:\d+: (.+)\n\s*--- SKIP: (\S+)|^SKIPPED \[\d+\] (.+)", re.MULTILINE)


def run_lane(name: str, steps: list[tuple[list[str], Path]], timeout: int) -> bool:
    """Runs a lane's commands in order and prints its output in one block; False if it failed or stopped."""
    log: list[str] = []
    started = time.monotonic()
    status = "PASS"
    for command, cwd in steps:
        if failed.is_set() and not keep_going:
            status = "STOPPED after another lane failed"
            break
        log.append(f"[{cwd.relative_to(ROOT)}] {' '.join(command)}")
        output, code = execute(command, cwd, timeout)
        # gofmt -l succeeds while listing the files that need formatting.
        if command[0] == "gofmt" and output:
            output, code = "Go formatting required:\n" + output, 1
        if output:
            log.append(output)
        for match in SKIP.finditer(output):
            reason, test, summary = match.groups()
            skipped.append(f"{name}: {summary or f'{test}: {reason}'}")
        if code != 0:
            failed.set()
            status = "FAIL"
            break
    with print_lock:
        print(f"=== {status} {name} ({time.monotonic() - started:.0f}s) ===", flush=True)
        if status == "FAIL":
            print("\n".join(log) + "\n", flush=True)
    return status == "PASS"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", action="store_true", help="run one lane at a time")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="lanes to run at once")
    parser.add_argument("--keep-going", action="store_true", help="run every lane even after a failure")
    parser.add_argument("--timeout", type=int, default=600, help="seconds before a stuck command fails")
    args = parser.parse_args()
    global keep_going
    keep_going = args.keep_going
    tools = {
        "golangci-lint": "go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0",
        "go": "install Go 1.25",
        "gofmt": "it comes with Go",
        "npm": "install Node.js 22",
    }
    for tool, how in tools.items():
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required: {how}")
    started = time.monotonic()
    work = lanes()
    with ThreadPoolExecutor(max_workers=1 if args.serial else max(1, args.jobs)) as pool:
        results = list(pool.map(lambda item: run_lane(item[0], item[1], args.timeout), work.items()))
    elapsed = time.monotonic() - started
    if skipped:
        print(
            "\nNOT RUN (the tests skipped themselves; run the database ones with scripts/db_tests.py, stack up):"
        )
        print("\n".join(f"  {line}" for line in skipped))
    if not all(results):
        raise SystemExit(f"FAIL: see the failing lane above ({elapsed:.0f}s)")
    print(f"\nPASS: all local quality gates ({elapsed:.0f}s)")


if __name__ == "__main__":
    main()
