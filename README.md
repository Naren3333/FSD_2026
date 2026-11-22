# Learning Ledger

A learning platform for schools. Teachers run classrooms, share teaching materials, set and mark assessments, and follow each student's progress. Students submit work, see released results, ask questions about their class's materials and practise from them. A school admin manages accounts and classrooms.

It is built as ten independently deployable applications (Go and Python) behind a Kong gateway and Keycloak sign-in, with a strict Angular web app, Kafka events, one PostgreSQL database per service, and AI features that all run through one billing-aware service. Everything runs locally with Docker Compose.

- [How it is built](docs/architecture.md): services, data ownership, events, security, the AI design and its limits.
- [Design decisions](docs/decisions.md): 26 numbered decisions with their reasons and evidence. Code comments cite them as `adr NNNN`.
- [DESIGN.md](DESIGN.md) and [UX-CONTRACT.md](UX-CONTRACT.md): the web app's visual design and the behaviour every screen must keep.

## What it does

| Who | What they can do |
|---|---|
| Teacher | Create classrooms and curriculum topics; upload PDF, PowerPoint, Word or Markdown materials; write assessments of up to 50 questions (multiple choice, numerical, short answer, rubric-marked free response), or generate draft questions and marking guidance from the materials; review AI marking suggestions and finalize grades; release results; reopen a finalized grade; keep a question bank and give chosen students a resit; ask an agent to interpret one student's results or answer a question about the class. |
| Student | Submit assessments; see results once released; see progress by topic; ask the materials questions and get cited answers; practise from chosen materials, with each answer key shown only after checking an answer. |
| School admin | Create teacher and student accounts, create and reassign classrooms, manage rosters, and open platform monitoring. |

Rules the system enforces:
- Grades stay drafts until the teacher finalizes them. AI suggestions never change a mark.
- Students never see answer keys, rubrics or unreleased grades, apart from the keys of their own practice questions.
- Every paid AI call is billed to the user who asked, within a daily allowance ($5 a student, $10 a teacher).

## The ten applications

| Application | Language | Owns |
|---|---|---|
| identity | Go | accounts, classrooms, enrollments, roles |
| curriculum | Go | topics and their prerequisites |
| content | Go | teaching materials, indexed passages, search |
| assessment | Go | assessments, questions, submissions, question bank, resits |
| grading | Go | grades, AI marking suggestions and their second check |
| analytics | Python | topic mastery from released grades |
| ai-core | Python | every model call, ingest, rerank, the spend ledger |
| question-chatbot-agent | Python | cited answers about class materials |
| question-generation-agent | Python | draft questions, marking guidance, student practice |
| student-performance-agent | Python | interpretations of a student's results, class insights |

Each owns its database and is reached only through its API or its events. `infra/` holds Kong, Keycloak, the Caddy edge and monitoring; `contracts/` holds every API and event schema; `apps/web` is the Angular app; `packages/` holds shared transport, auth and operational helpers only.

## Run it locally

### 1. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) with Compose v2 | current | the stack (about twenty containers) |
| Python | 3.12+ (CI uses 3.13) | setup and helper scripts |
| Git | any | the code |

On Windows, run the Python scripts with `py -3.13` in place of `python`. Keep the repository's LF line endings (`.gitattributes`).

### 2. Generate local secrets (once per device)

```sh
python scripts/setup.py
```

This writes `.env` (random passwords for every database, service account and local user), `.local/postgres-init.sql` and `.local/learning-realm.json`. It refuses to run if `.env` exists, so credentials are never overwritten. Never commit `.env` or `.local/`, and keep `.env` with its Docker volumes: the databases were created with those passwords.

### 3. Add AI keys (optional)

The AI features call real providers and cost money on your own keys. In `.env`:

```sh
OPENAI_API_KEY=sk-...
VOYAGE_API_KEY=...      # reranking; without it chat and question generation refuse rather than guess
```

Without keys the rest of the app works: uploads wait in indexing and the AI features say they are unavailable.

### 4. Start

```sh
docker compose up -d --build
python scripts/wait_ready.py
```

The first build takes several minutes. `wait_ready.py` returns once every application and monitoring answers.

### 5. Trust the local certificate (once per device)

The edge serves the app, the gateway (`/api`) and sign-in over HTTPS from its own local authority:

```sh
docker compose cp edge:/data/caddy/pki/authorities/local/root.crt .local/edge-root.crt
```

- Windows: `certutil -user -addstore Root .local/edge-root.crt`
- macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain .local/edge-root.crt`
- Linux: import it as a certificate authority in your browser.

Restart the browser afterwards. This is needed again only if the `edge-data` volume is removed.

### 6. Sign in

Open [https://app.localhost](https://app.localhost) (sign-in is at `https://auth.localhost`).

| Username | Role | Password in `.env` |
|---|---|---|
| `admin` | school admin | `ADMIN_PASSWORD` |
| `teacher` | teacher | `TEACHER_PASSWORD` |
| `other-teacher` | a second teacher | `OTHER_TEACHER_PASSWORD` |
| `student` | student | `STUDENT_PASSWORD` |

`python scripts/seed_students.py` adds thirty more students who share one generated password, printed at the end and kept in `.local/seed-students.json`.

### A first walk-through

1. Sign in as the student once, so the account exists.
2. As the teacher, create a classroom and enroll the student, add a curriculum topic and publish an assessment.
3. As the student, select the classroom and submit the assessment.
4. As the teacher, review the grade, finalize and release it, then open Learning performance. The student sees it under My results and My progress.
5. With AI keys: upload a teaching material and wait for it to show as indexed. Then ask about it under Ask the materials, generate questions from it in the assessment editor, or, as the student, practise from it under Practice.

### Everyday commands

```sh
docker compose down               # stop, keeping all data
docker compose up -d              # start again
docker compose up -d --build      # after pulling changes
python scripts/upgrade_local.py   # when Compose asks for it: adds missing settings, changes nothing that exists
```

`docker compose down -v` deletes all local data (classrooms, materials, grades, chat history, the AI cache and the certificate authority) and cannot be undone.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.py` says `.env already exists` | Setup already ran. To start over: `docker compose down -v`, delete `.env` and `.local/`, run setup again. |
| Port 8000 or 8081 is in use | Set `KONG_PORT` or `KEYCLOAK_PORT` in `.env`, then `docker compose up -d`. |
| Port 443 is in use | Stop whatever holds it (often IIS or another local proxy); sign-in redirects are registered for `https://app.localhost` on 443. |
| `app.localhost` does not open | Use a current Chrome, Edge or Firefox, which resolve `*.localhost`; check `docker compose ps` shows `edge` running. |
| `wait_ready.py` times out | Check `docker compose ps` and `docker compose logs <service>`; a slow first build may need a second run. |

## Tests and checks

Host tools: Go 1.25+, Node 22.12+ (CI uses 22.17), golangci-lint v2.14 (`go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0`), and a virtual environment with the pinned Python tools:

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.lock
cd apps/web && npm ci && npx playwright install chromium && cd ../..
```

```sh
python scripts/check.py           # format, lint, types, unit tests, builds, contracts; no stack needed
python scripts/stack_tests.py     # integration, database and browser tests in a disposable stack
```

- `check.py` runs its lanes in parallel (`--serial`, `--keep-going`) and fails if `contracts/` drift from what `scripts/export_contracts.py` generates. GitHub Actions runs the same lanes.
- `stack_tests.py` starts a separate Compose project, `learning-test`, on its own ports (edge 18443, Kong 18000, Keycloak 18081), so the development stack and its data are never touched. Name `integration`, `db` or `e2e` to run only those; `--live-ai` uses the AI keys and makes real paid calls.
- `contracts/` is the single description of every API. The web app's types are generated from it, each Go service tests its JSON against it, and the integration suite validates every response it receives.

### AI evals (paid)

`scripts/eval/` holds manual evals of the AI features, run against the development stack with real keys. Each run is capped with `--max-usd` (default $0.50), reads its cost from ai-core's ledger and checkpoints its results in `.local/eval-runs/`. The test documents are not in the repository: put them in `../fsd_test/data/test_pdfs` or set `EVAL_PDF_DIR`, then load them once with `python scripts/eval/load_corpus.py`.

| Eval | Run | Measures |
|---|---|---|
| Retrieval | `run_golden.py --rerank` | hit@1, MRR and recall on a golden question set, and leaks across classrooms |
| Chat | `run_chat.py`, `--probe-only` | grounded, cited answers, refusals, injections, multi-topic coverage; the answer check's catch and false-alarm rates on planted faults |
| Question generation | `run_generation.py`, `--practice` | returned and passed questions, reviewer catches on planted faults, latency and calls |
| Marking | `load_marking.py`, then `run_marking.py` | agreement with expected levels and how often the second check doubts a wrong first marking |
| Performance and class insights | `load_performance.py` / `run_performance.py`, `load_class_insights.py` / `run_class_insights.py` | grounded findings, citations and recommendations |

The latest results and what they decided are recorded with the matching decision in [docs/decisions.md](docs/decisions.md).

## Status

This is a complete local system, not a production deployment. Before production it would need real certificates, Kafka TLS and access control, per-service machine identities, a sign-in realm without the password-grant test client and with brute-force protection, backups with tested restores, managed secrets and dependency audits. [docs/architecture.md](docs/architecture.md) lists the known limits.
