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

## Development

Host checks also require Go 1.25+ and Node 22.12+. Create and activate a Python virtual environment, then run:

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

## Current scope

This is a local development foundation, not production ready. AI features return `501 Not Implemented`; PDF extraction is unsupported. The assessment UI supports one question per assessment.

See [architecture](docs/architecture/system.md), [operations](docs/development/operations.md), and [handover](docs/development/handover.md) for technical details and remaining work.
