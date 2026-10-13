# Learning Ledger

A learning platform for classrooms, content uploads, assessments, teacher-approved grading, and student performance tracking. Built with Angular and ten Go/Python services.

## Run locally

Requires Docker Desktop running with Compose v2 and Python 3.12+.

```sh
python scripts/setup.py
docker compose up -d --build
```

Open [localhost:4200](http://localhost:4200). The first build may take several minutes.

Sign in as `teacher` or `student`. Find their passwords in `.env` under `TEACHER_PASSWORD` and `STUDENT_PASSWORD`. Never commit `.env` or `.local/`.

Run setup only once; keep generated credentials with the existing Docker volumes. To stop the app while keeping data:

```sh
docker compose down
```

## Basic workflow

1. Sign in as a student and copy the ID shown in the header.
2. Sign in as a teacher, create a classroom, and enroll that student ID.
3. Add a curriculum skill and create an assessment.
4. As the student, select the classroom and submit the assessment.
5. As the teacher, review and finalize the grade, then view Learning performance.
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
python scripts/integration.py
cd apps/web
npm run test:e2e
```

Integration and browser tests require the Docker stack to be running.

`check.py` fails on unformatted web code; run `npm run format` in `apps/web` to fix it. Component templates live in `.html` files next to their components.

## Current scope

This is a local development foundation, not production ready. PDFs are indexed and document chat answers from them with sources once `OPENAI_API_KEY` (and, for reranking, `VOYAGE_API_KEY`) are set in `.env`; without them uploads wait in indexing and chat answers fail with a clear message. AI question generation and performance interpretation return `501 Not Implemented`. The assessment UI supports one question per assessment.

See [architecture](docs/architecture/system.md), [operations](docs/development/operations.md), and [handover](docs/development/handover.md) for technical details and remaining work.
