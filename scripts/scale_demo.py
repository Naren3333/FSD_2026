"""compare submission throughput across replica counts and a worker failure on the test stack."""

import argparse
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from local_stack import (
    BASE,
    ROOT,
    admin_client,
    check,
    ensure_person,
    env,
    password_token,
    psql_value,
    require_test_stack,
)

SCALED = ["identity", "assessment", "grading"]  # the services docker-compose.scale.yml replicates
WORK = ROOT / ".local" / "scale-demo"  # run.json for k6, and the report
MARKED_TIMEOUT = 180  # seconds for every submission of a run to be marked
WARM_UP_REQUESTS = 200  # reads spread over the last tokens, a few per session
TOKEN_SECONDS = 120  # how long the script uses one teacher token; keycloak's last five minutes
TEACHER_BUDGET = 100  # requests per teacher token: kong allows each token 120 a minute
STUDENT_PASSWORD_KEY = "STUDENT_PASSWORD"  # the load students share the local student's password
QUESTIONS = [
    {"prompt": "0.1 + 0.2?", "type": "numerical", "answer": "0.3", "tolerance": "0", "options": []},
    {
        "prompt": "Which is a fraction?",
        "type": "multiple_choice",
        "answer": "1/2",
        "tolerance": "",
        "options": ["1/2", "word"],
    },
]


@dataclass
class Run:
    label: str
    replicas: int
    kill: bool = False
    k6: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)  # what k6 said about each failed submission
    accepted_seconds: float = 0.0  # from the first submission stored to the last
    marked_seconds: float | None = None  # from the first submission stored to the last one marked
    requests: dict[str, int] = field(default_factory=dict)  # requests each replica served during the run
    consumers: list[str] = field(default_factory=list)
    killed: str = ""
    checks: dict[str, bool] = field(default_factory=dict)


# --- stack
# -------------------------------------------------------------------------------------------------------


def compose(*args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=timeout,
    )


def containers(service: str) -> list[str]:
    """the names of a service's running containers."""
    out = compose("ps", "--status", "running", "--format", "{{.Name}}", service).stdout
    return sorted(out.split())


def scale(replicas: int) -> None:
    """runs `replicas` of each scaled service and waits until every one of them is healthy."""
    flags = [arg for service in SCALED for arg in ("--scale", f"{service}={replicas}")]
    compose(
        "up", "--detach", "--no-build", "--no-recreate", "--wait", "--wait-timeout", "300", *flags, *SCALED
    )
    for service in SCALED:
        running = containers(service)
        if len(running) != replicas:
            raise SystemExit(f"{service} runs {len(running)} replicas, not {replicas}")
    # kong re-resolves service names every 5 s (kong_dns_valid_ttl), so it sees the new replicas after that.
    time.sleep(6)


def requests_served(names: list[str], since: str) -> dict[str, int]:
    """how many requests each container logged since an rfc 3339 time: the go services log one line per request."""
    served = {}
    for name in names:
        logs = subprocess.run(
            ["docker", "logs", "--since", since, name],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=60,
        )
        served[name] = (logs.stdout + logs.stderr).count('"msg":"request"')
    return served


def consumer_group(group: str) -> list[dict[str, str]]:
    """each partition of a consumer group's topic: its lag, and the container whose consumer holds it."""
    out = compose(
        "exec", "-T", "kafka", "/opt/kafka/bin/kafka-consumer-groups.sh",
        "--bootstrap-server", "localhost:9092", "--describe", "--group", group,
    ).stdout  # fmt: skip
    # a consumer id is "app@<container hostname> (github.com/segmentio/kafka-go)-<uuid>"; the hostname is
    # the container's short id.
    ids = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    names = dict(line.split(" ", 1) for line in ids.splitlines() if " " in line)
    rows = []
    for line in out.splitlines():
        found = re.match(rf"{re.escape(group)}\s+\S+\s+(\d+)\s+\S+\s+\S+\s+(\S+)\s+(.*)", line)
        if found:
            host = re.search(r"app@([0-9a-f]+)", found[3])
            rows.append(
                {
                    "partition": found[1],
                    "lag": found[2],
                    "holder": names.get(host[1], host[1]) if host else "unassigned",
                }
            )
    return rows


# --- people and data
# ---------------------------------------------------------------------------------------------


class Teacher:
    """the local teacher, signing in again before kong's per-token limit is reached."""

    def __init__(self) -> None:
        self.token = ""
        self.used = TEACHER_BUDGET
        self.signed_in = 0.0

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        # keycloak's access tokens last five minutes; sign in again well before that, or before kong's
        # limit.
        if self.used >= TEACHER_BUDGET or time.monotonic() - self.signed_in > TOKEN_SECONDS:
            self.token = password_token("teacher", env()["TEACHER_PASSWORD"])
            self.used, self.signed_in = 0, time.monotonic()
        self.used += 1
        headers = {"Authorization": "Bearer " + self.token}
        response = check(httpx.request(method, BASE + path, headers=headers, timeout=30, **kwargs))
        return response.json() if response.content else None


def create_students(count: int) -> list[str]:
    """creates the load students in keycloak and signs each in once, which registers them with identity."""
    password = env()[STUDENT_PASSWORD_KEY]
    names = [f"load-student-{i:04d}" for i in range(1, count + 1)]

    def create(batch: list[str]) -> None:
        # keycloak's admin tokens last a minute, so each batch gets its own.
        with admin_client() as admin:
            for name in batch:
                ensure_person(admin, name, "Load", name.rsplit("-", 1)[1], password)
        for name in batch:
            headers = {"Authorization": "Bearer " + password_token(name, password)}
            check(httpx.post(BASE + "/identity/me", json={}, headers=headers, timeout=30))

    batches = [names[i : i + 10] for i in range(0, count, 10)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(create, batches))
    return names


def sign_in_all(names: list[str], sessions: int) -> list[str]:
    """`sessions` fresh tokens per student, the first len(names) one per student in order."""
    password = env()[STUDENT_PASSWORD_KEY]
    with ThreadPoolExecutor(16) as pool:
        return list(pool.map(lambda name: password_token(name, password), names * sessions))


def classroom(teacher: Teacher, students: list[str]) -> str:
    room = teacher.request("POST", "/identity/classrooms", json={"name": f"Scale demo {uuid4().hex[:8]}"})
    for name in students:
        teacher.request("POST", f"/identity/classrooms/{room['id']}/enrollments", json={"username": name})
    return str(room["id"])


def assessment(teacher: Teacher, room: str, topic: str, label: str) -> dict[str, Any]:
    questions = [{**q, "topic_id": topic} for q in QUESTIONS]
    created: dict[str, Any] = teacher.request(
        "POST",
        "/assessment/assessments",
        json={"classroom_id": room, "title": f"Deadline burst: {label}", "questions": questions},
    )
    return created


# --- one run
# -----------------------------------------------------------------------------------------------------


def run_k6(run_file: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """runs scripts/load/deadline.js and returns its summary, and the failed submissions it reported."""
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / "run.json"
    path.write_text(json.dumps(run_file), encoding="utf-8")
    path.chmod(0o644)  # the k6 image runs as its own user
    try:
        done = compose(
            "run", "--rm", "--no-deps", "-T",
            "-e", f"BROWSE_RATE={args.browse_rate}", "-e", f"BROWSE_SECONDS={args.browse_seconds}",
            "k6", "run", "--quiet", "--no-color", "/scripts/deadline.js",
        )  # fmt: skip
    finally:
        path.unlink()  # it holds signed-in tokens
    summary: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    failures = [line for line in done.stderr.splitlines() if "submit failed" in line]
    return summary, failures


def counts(assessment_id: str) -> tuple[int, int]:
    """how many submissions of an assessment assessment has stored, and how many of them grading has marked."""
    where = f"assessment_id='{UUID(assessment_id)}'"
    stored = psql_value(
        f"SELECT count(*) FROM submissions WHERE {where}", database="assessment", user="assessment"
    )
    marked = psql_value(f"SELECT count(*) FROM evaluations WHERE {where}", database="grading", user="grading")
    return int(stored), int(marked)


def kill_grading_replica_when_submitting(assessment_id: str, students: int, run: Run) -> None:
    """kills the last grading replica as soon as the first submission is stored, so it dies with its share of the burst still to mark."""
    deadline = time.monotonic() + MARKED_TIMEOUT
    while time.monotonic() < deadline and counts(assessment_id)[0] == 0:
        time.sleep(0.1)
    victim = containers("grading")[-1]
    subprocess.run(["docker", "kill", victim], capture_output=True, check=True, timeout=60)
    run.killed = f"{victim} as the burst began, with {counts(assessment_id)[1]} of {students} marked"


def warm_up(paths: list[str], tokens: list[str]) -> None:
    """a few reads through kong first, so each fresh replica has opened its database connections before the burst, as a replica that has been serving would have."""
    with ThreadPoolExecutor(30) as pool:
        list(
            pool.map(
                lambda i: httpx.get(
                    BASE + paths[i % len(paths)],
                    headers={"Authorization": "Bearer " + tokens[-1 - i]},
                    timeout=30,
                ),
                range(WARM_UP_REQUESTS),
            )
        )


def wait_marked(assessment_id: str) -> bool:
    """waits until grading has marked every submission assessment stored."""
    deadline = time.monotonic() + MARKED_TIMEOUT
    while time.monotonic() < deadline:
        stored, marked = counts(assessment_id)
        if marked >= stored:
            return True
        time.sleep(0.5)
    return False


def timings(run: Run, assessment_id: str) -> None:
    """from the databases' own clocks: how long the burst took to store, and to mark."""
    where = f"assessment_id='{UUID(assessment_id)}'"
    first, last = psql_value(
        f"SELECT extract(epoch FROM min(created_at)) || ',' || extract(epoch FROM max(created_at)) "
        f"FROM submissions WHERE {where}",
        database="assessment",
        user="assessment",
    ).split(",")
    marked = psql_value(
        f"SELECT extract(epoch FROM max(created_at)) FROM evaluations WHERE {where}",
        database="grading",
        user="grading",
    )
    run.accepted_seconds = float(last) - float(first)
    run.marked_seconds = float(marked) - float(first)


def verify(run: Run, assessment_id: str, students: int) -> None:
    """what must hold however many replicas ran: each student's submission accepted once and marked exactly once, the outbox drained and grading's consumers caught up."""
    where = f"assessment_id='{UUID(assessment_id)}'"
    stored = psql_value(
        f"SELECT count(*) FROM submissions WHERE {where}", database="assessment", user="assessment"
    )
    # an evaluation's id is its submission's id, so a submission marked twice would be a second row for one
    # id.
    evaluations = psql_value(
        f"SELECT count(*) || ',' || count(DISTINCT id) || ',' || count(DISTINCT student_id) FROM evaluations "
        f"WHERE {where}",
        database="grading",
        user="grading",
    )
    unpublished = psql_value(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL", database="assessment", user="assessment"
    )
    group = consumer_group("grading-submitted-v4")
    n = str(students)
    run.checks = {
        f"all {students} submissions accepted": run.k6["submit"]["passes"] == students and stored == n,
        "each marked exactly once": evaluations == f"{n},{n},{n}",
        "outbox drained": unpublished == "0",
        "no consumer lag": all(row["lag"] in {"0", "-"} for row in group),
    }
    run.consumers = [f"partition {row['partition']}: {row['holder']}" for row in group]


def one_run(
    run: Run, teacher: Teacher, room: str, topic: str, students: list[str], args: argparse.Namespace
) -> None:
    print(f"\n--- {run.label} ---", flush=True)
    scale(run.replicas)
    replicas = [name for service in SCALED for name in containers(service)]
    created = assessment(teacher, room, topic, run.label)
    first, second = (q["id"] for q in created["questions"])
    read_paths = [
        f"/assessment/assessments?classroom_id={room}",
        f"/assessment/assessments/{created['id']}",
        f"/assessment/assessments/{created['id']}/submission",
        f"/grading/evaluations/mine?classroom_id={room}",
    ]
    tokens = sign_in_all(students, args.sessions)
    run_file = {
        "students": len(students),
        "submit_path": f"/assessment/assessments/{created['id']}/submissions",
        "answers": {first: "0.30", second: "1/2"},
        "read_paths": read_paths,
        "tokens": tokens,
    }
    warm_up(read_paths, tokens)
    killer = None
    if run.kill:
        killer = threading.Thread(
            target=kill_grading_replica_when_submitting, args=(created["id"], len(students), run), daemon=True
        )
        killer.start()
    since = datetime.now(UTC).isoformat()
    run.k6, run.failures = run_k6(run_file, args)
    if killer:
        killer.join(timeout=MARKED_TIMEOUT)
    if wait_marked(created["id"]):
        timings(run, created["id"])
    run.requests = requests_served(replicas, since)
    verify(run, created["id"], len(students))
    print(report_run(run), flush=True)


# --- report
# ------------------------------------------------------------------------------------------------------


def ms(trend: dict[str, float] | None, key: str) -> str:
    return f"{trend[key]:.0f} ms" if trend else "-"


def report_run(run: Run) -> str:
    k6 = run.k6
    browse = k6["browse"]
    reads = browse["passes"] + browse["fails"]
    accepted = k6["submit"]["passes"]
    lines = [
        f"### {run.label}",
        "",
        f"- Submissions: {accepted} accepted, {k6['submit']['fails']} failed; each took p50 "
        f"{ms(k6['submit']['ms'], 'p50')}, p95 {ms(k6['submit']['ms'], 'p95')}; all stored within "
        f"{run.accepted_seconds:.2f} s ({accepted / max(run.accepted_seconds, 0.001):.0f} a second)",
        "- All marked "
        + (f"{run.marked_seconds:.2f} s after the first was stored" if run.marked_seconds else "NOT in time"),
        f"- Reads alongside: {reads} in {browse['seconds']} s ({reads / browse['seconds']:.0f} a second), "
        f"{browse['fails']} failed; p50 {ms(browse['ms'], 'p50')}, p95 {ms(browse['ms'], 'p95')}; "
        f"{k6['dropped_iterations']} not started because responses fell behind",
        f"- Server errors: {k6['server_errors']}; rate limited: {k6['rate_limited']}",
    ]
    lines += [f"- {failure}" for failure in run.failures[:5]]
    if run.killed:
        lines.append(f"- Killed {run.killed}")
    lines.append("- Requests each replica served:")
    lines += [f"  - {name}: {count}" for name, count in run.requests.items()]
    lines.append("- Grading's submission consumers afterwards: " + "; ".join(run.consumers))
    lines += [f"- {'PASS' if ok else 'FAIL'} {name}" for name, ok in run.checks.items()]
    return "\n".join(lines)


def report(runs: list[Run], args: argparse.Namespace) -> str:
    header = [
        "# Scale demo",
        "",
        f"{args.students} students submit one assessment at once while the class reads at {args.browse_rate} "
        f"requests a second for {args.browse_seconds} s, from {args.sessions} signed-in sessions each. Identity, Assessment and Grading each run as the replicas "
        "named, every replica capped at the CPU docker-compose.scale.yml gives it (REPLICA_CPUS). Everything, the "
        "load generator included, ran on one computer and one PostgreSQL, so these numbers show the services scaling "
        "out correctly, not the capacity of a production deployment.",
        "",
    ]
    return "\n".join(header + [report_run(run) + "\n" for run in runs])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--students", type=int, default=200)
    parser.add_argument("--browse-rate", type=int, default=600, help="reads a second during each run")
    parser.add_argument("--browse-seconds", type=int, default=30)
    parser.add_argument("--sessions", type=int, default=4, help="signed-in sessions (tokens) per student")
    parser.add_argument("--dev", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.browse_rate * min(args.browse_seconds, 60) / (args.students * args.sessions) > 100:
        parser.error("that many reads per session would reach Kong's limit of 120 a minute per token")
    require_test_stack(dev=args.dev)
    if not containers("kafka"):
        raise SystemExit("The stack is not running.")

    teacher = Teacher()
    teacher.request("POST", "/identity/me", json={})
    students = step(f"Creating {args.students} students", lambda: create_students(args.students))
    room = step("Enrolling them", lambda: classroom(teacher, students))
    topic = str(
        teacher.request("POST", "/curriculum/topics", json={"name": f"Scale demo {uuid4().hex[:8]}"})["id"]
    )

    runs = [
        Run("1 replica each", 1),
        Run("3 replicas each", 3),
        Run("3 replicas each, one Grading replica killed as the burst begins", 3, kill=True),
    ]
    for run in runs:
        one_run(run, teacher, room, topic, students, args)

    text = report(runs, args)
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "report.md").write_text(text, encoding="utf-8")
    print(f"\nReport written to {(WORK / 'report.md').relative_to(ROOT)}")
    failed = [f"{run.label}: {name}" for run in runs for name, ok in run.checks.items() if not ok]
    if failed:
        raise SystemExit("FAIL: " + "; ".join(failed))
    print("PASS: every run accepted and marked every submission exactly once")


def step[R](label: str, action: Callable[[], R]) -> R:
    print(f"{label}...", flush=True)
    started = time.monotonic()
    result = action()
    print(f"  done ({time.monotonic() - started:.0f}s)", flush=True)
    return result


if __name__ == "__main__":
    main()
