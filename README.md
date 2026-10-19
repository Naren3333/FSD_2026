# Learning Ledger

A learning platform for classrooms, content uploads, assessments, teacher-approved grading, and student performance tracking. Built with Angular and ten Go/Python services.

## Run locally

Requires Docker Desktop running with Compose v2 and Python 3.12+. On Windows, run the scripts with `py -3.13` in place of `python`.

```sh
python scripts/setup.py
docker compose up -d --build
python scripts/wait_ready.py
```

Open [localhost:4200](http://localhost:4200) once `wait_ready.py` passes. The first build may take several minutes.

If your `.env` was made by an older `setup.py` (Compose then stops with "Run scripts/setup.py or scripts/upgrade_local.py"), run `python scripts/upgrade_local.py`: it adds the missing values to `.env` without changing existing ones, and with the stack up it also adds new databases and Keycloak clients. See [operations](docs/development/operations.md).

Sign in as `teacher` or `student`. Find their passwords in `.env` under `TEACHER_PASSWORD` and `STUDENT_PASSWORD`. Never commit `.env` or `.local/`.

Run setup only once; keep generated credentials with the existing Docker volumes. To stop the app while keeping data:

```sh
docker compose down
```

## Basic workflow

1. Sign in as a student once; the header shows their username.
2. Sign in as a teacher, create a classroom, and enroll the student from All students (search by name or that username). The classroom's students are listed above it.
3. Add a curriculum topic and create an assessment.
4. As the student, refresh classrooms in the header, select the classroom and submit the assessment.
5. As the teacher, review and finalize the grade, then view Learning performance. The student sees the finalized grade under My results and their topics under My progress.
6. As the teacher, upload a PDF under Teaching materials and wait for it to show as indexed. Then, as the teacher or the student, ask about it under Ask the materials. Each provider call costs money on your API keys. Each user has a daily AI allowance ($5 for a student, $10 for a teacher), shown in the header.

## Development

Host checks also require Go 1.25+, Node 22.12+ and golangci-lint v2.14 (`go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0`). Lint rules live in `ruff.toml` and `.golangci.yml` at the repository root. Create and activate a Python virtual environment, then run:

```sh
python -m pip install -r requirements-dev.txt
cd apps/web
npm ci
npx playwright install chromium
cd ../..
python scripts/check.py
python scripts/stack_tests.py
```

`stack_tests.py` runs the integration, database and browser tests in a fresh, disposable `learning-test` stack and removes it afterwards, so they never touch the development stack's data; it publishes its own ports (Kong 18000, Keycloak 18081, the web app 14200), so a running development stack is left alone (docs/development/operations.md, "Test stack"). `check.py` needs no stack, and its web tests can time out while the stack loads the machine. `check.py` runs its checks in parallel (`--serial` runs them one at a time, `--keep-going` reports every failure instead of stopping at the first) and lists any tests that skipped themselves. It also fails if any contract in `contracts/` differs from what `scripts/export_contracts.py` produces, which regenerates them. The contracts are the one description of each API: the five Python services' come from their FastAPI apps, and the five Go services' contracts and the event schemas are written inside `export_contracts.py`, with named schemas (`components/schemas`). Everything else follows them: the web app's API types are generated from them (`npm run contracts` in `apps/web`, which `export_contracts.py` runs and `check.py` checks), each Go service's `contract_test.go` checks its JSON structs against them field by field, and `integration.py` validates every JSON response it receives against them.

`check.py` fails on unformatted web code; run `npm run format` in `apps/web` to fix it. Component templates live in `.html` files next to their components.

## Current scope

This is a local development foundation, not production ready. PDFs are indexed and document chat answers from them with sources once `OPENAI_API_KEY` (and, for reranking, `VOYAGE_API_KEY`) are set in `.env`; without them uploads wait in indexing and chat answers fail with a clear message. Teachers can generate draft questions and marking guidance from a classroom's indexed PDFs (ADR 0006), which they review before anything is saved; performance interpretation returns `501 Not Implemented`. Assessments have up to 50 questions (multiple choice, numerical, short answer and rubric-marked free response); grades stay drafts until the teacher finalizes them, AI marking suggestions never change a mark, and students see results only once released.

See [architecture](docs/architecture/system.md), [scalability](docs/architecture/scalability.md), [operations](docs/development/operations.md), and [handover](docs/development/handover.md) for technical details and remaining work.
