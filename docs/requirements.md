# CODEX MASTER PROMPT

## AI-Powered Personalized Learning Platform

### Microservices Foundation — Go, Python, Angular, Kong, Kafka and Neon

You are acting as a Principal Software Architect, Staff Backend Engineer, Distributed Systems Engineer, Database Engineer, Security Engineer, and Technical Lead.

Your task is to build the foundational architecture for an AI-powered personalized learning platform.

**This project must use genuine, independently deployable microservices.**

Your immediate objective is to implement the microservice architecture, infrastructure, API contracts, data ownership, event-driven communication, and non-AI functionality.

Do not implement actual artificial intelligence, LLM integrations, embeddings, or RAG inference during this milestone.

Instead, establish clean, well-defined integration points for future AI functionality.

The result must be a working, professionally engineered foundation—not a collection of empty directories and disconnected services.

---

# 1. CRITICAL: PRESERVE EXISTING WORK

Before making any changes:

1. Inspect the entire existing repository.
2. Read its README, AGENTS.md, architecture documentation, configuration, database migrations, and tests.
3. Identify all existing functionality and infrastructure.
4. Determine which components can be preserved.
5. Identify architectural conflicts with the requirements below.
6. Produce a concise migration or implementation plan.

Do not delete, overwrite, or rewrite existing code unnecessarily.

Do not migrate existing services to another language simply because the preferred language differs.

If the repository contains an existing NestJS implementation, assess whether it can be retained, adapted, or incrementally replaced.

Preserve working behavior and data throughout any necessary migration.

Do not introduce destructive database operations without explicit authorization.

Proceed with implementation after completing the inspection and documenting your decisions.

---

# 2. PRODUCT REQUIREMENTS

The platform has four main features.

## Feature 1 — Content-based question-answering

Students ask questions about educational content uploaded by their teachers.

Eventually, the system will use RAG to retrieve relevant content and generate grounded explanations with source references.

## Feature 2 — Student performance evaluation

The platform evaluates student assessment performance and identifies potential learning gaps.

Calculations must be based on actual assessment evidence, not invented AI-generated scores.

## Feature 3 — Individualized teacher feedback

Teachers receive student-level performance summaries, evidence of weaknesses, and suggested instructional interventions.

AI-generated recommendations will be implemented in a future milestone.

## Feature 4 — Personalized practice generation

Students receive practice questions based on uploaded content, curriculum skills, and demonstrated weaknesses.

AI-generated questions will be implemented later.

For now, the system must support the underlying curriculum, question, assessment, grading, and performance data structures.

---

# 3. REQUIRED ARCHITECTURE: EXACTLY 10 MICROSERVICES

Implement the following ten independently deployable application services.

| #  | Service                           | Language |
| -- | --------------------------------- | -------- |
| 1  | Identity & Classroom Service      | Go       |
| 2  | Curriculum Service                | Go       |
| 3  | Content Service                   | Go       |
| 4  | Assessment Service                | Go       |
| 5  | Grading Service                   | Go       |
| 6  | Learning Analytics Service        | Python   |
| 7  | AI Service                        | Python   |
| 8  | Question Generation Agent Service | Python   |
| 9  | Student Performance Agent Service | Python   |
| 10 | Question Chatbot Agent Service    | Python   |

These are ten separate services, not ten modules inside one backend.

Each must have:

* An independently runnable application.
* Its own Dockerfile.
* Independent configuration.
* Explicit API or event contracts.
* Health and readiness endpoints.
* An independently executable test suite.
* Clear responsibility boundaries.
* Independent deployment capability.

Business services that persist data must have explicitly owned database schemas and credentials.

Stateless services should not be given databases merely to satisfy architectural symmetry.

Kong, Kafka, Neon, and the frontend are not additional business microservices.

Do not introduce an eleventh business service without a documented, compelling reason.

---

# 4. LANGUAGE SELECTION AND JUSTIFICATION

Use languages according to the responsibilities of each service.

## Go: Core transactional services

Use Go for:

* Identity & Classroom.
* Curriculum.
* Content.
* Assessment.
* Grading.

These services primarily perform structured operations, transactional persistence, authorization, domain validation, and concurrent request handling.

Go provides strong static typing, a straightforward concurrency model, efficient HTTP handling, and relatively small deployment artifacts.

Recommended starting tools:

* Go standard library HTTP routing or a lightweight router such as chi.
* pgx for PostgreSQL access.
* sqlc for type-safe SQL where appropriate.
* golang-migrate or an equivalent migration tool.
* Go standard testing facilities.
* slog for structured logging.

Select compatible, maintained versions and justify deviations.

Do not introduce a heavyweight framework without a demonstrated need.

Do not create a generic repository abstraction for every database query.

Use explicit interfaces where they protect meaningful architectural boundaries.

## Python: Analytics and AI-related services

Use Python for:

* Learning Analytics.
* AI Service.
* Question Generation Agent.
* Student Performance Agent.
* Question Chatbot Agent.

Python is appropriate because the eventual platform will require statistical analysis, retrieval tooling, model integrations, and machine-learning libraries.

Use FastAPI and Pydantic for HTTP APIs and request validation.

Keep deterministic analytics independent of LLM functionality.

Do not introduce LangChain, LangGraph, CrewAI, or other agent frameworks during this milestone.

The AI services only need stable contracts and functioning non-AI infrastructure.

## Angular: Frontend

Use Angular with strict TypeScript.

Use feature-based organization, typed API clients, reactive forms, and appropriate Signals or RxJS state management.

Do not add React, Next.js, or another competing frontend framework.

## Other languages

Do not introduce another backend language unless there is a specific, measurable technical requirement that Go and Python cannot reasonably satisfy.

If another language is introduced, document:

* Why the language is necessary.
* Which problem it solves.
* Why Go and Python are insufficient for that problem.
* The maintenance and operational consequences.

Avoid polyglot complexity without a clear benefit.

---

# 5. HIGH-LEVEL SYSTEM ARCHITECTURE

The intended architecture is:

```text
                      Angular Frontend
                             |
                             v
                      Kong API Gateway
                             |
          +------------------+------------------+
          |                  |                  |
          v                  v                  v
   Identity Service   Curriculum Service   Content Service
          |                  |                  |
          +------------------+------------------+
                             |
                 +-----------+-----------+
                 |                       |
                 v                       v
          Assessment Service       Grading Service
                 |                       |
                 +-----------+-----------+
                             |
                             v
                  Learning Analytics
                             |
                             v
                   AI Service (future)
                             |
             +---------------+---------------+
             |               |               |
             v               v               v
         Question         Student          Question
        Generation       Performance       Chatbot
       Agent Service    Agent Service    Agent Service
```

This is a conceptual architecture, not a required chain of network calls.

Every service communicates only with the services it actually needs.

Avoid routing unrelated requests through the AI Service.

Avoid circular service dependencies.

Prefer asynchronous events for downstream reactions and explicit APIs for immediate operations.

Do not create synchronous request chains spanning numerous services.

---

# 6. SERVICE RESPONSIBILITIES

## Service 1 — Identity & Classroom

Language: Go.

Owns:

* Application user identities.
* Teacher and student profiles.
* Schools or organizations.
* Classrooms.
* Classroom membership.
* Enrollment.
* Application roles and access policies.

Implement functioning classroom and membership operations.

Integrate with a standards-based authentication solution.

Do not create your own password hashing or authentication protocol.

Other services must not query this service's private database tables.

They may validate authenticated claims and use explicit APIs for additional authorization information.

## Service 2 — Curriculum

Language: Go.

Owns:

* Subjects.
* Courses.
* Units.
* Topics.
* Skills.
* Learning objectives.
* Prerequisite relationships.

Implement functioning curriculum CRUD operations and appropriate validation.

Provide stable identifiers that other services can reference.

Do not duplicate curriculum ownership across services.

## Service 3 — Content

Language: Go.

Owns:

* Uploaded educational documents.
* Document metadata.
* Document access permissions.
* Content processing status.
* Document revisions.
* Extracted text and chunks.
* Knowledge-base metadata.
* Lesson plans and teaching materials.
* Chat conversation persistence where required.

Implement a working document-upload and metadata workflow.

Use appropriate object storage for original files.

An S3-compatible abstraction with MinIO for local development is acceptable.

Support at least plain-text educational files initially. Support PDF extraction if a reliable extractor is available within scope; otherwise, implement PDF upload validation and explicitly report extraction as unsupported until the processing capability is implemented.

Never claim that a document is indexed if embeddings have not actually been generated.

Create a documented future integration point for RAG.

The Content Service owns document authorization and retrieval access rules.

## Service 4 — Assessment

Language: Go.

Owns:

* Question banks.
* Questions.
* Answer keys.
* Assessments.
* Assignments.
* Student submissions.
* Question-to-skill mappings.
* Assessment lifecycle.

Implement functioning manually authored questions and assessments.

Support an initial set of objective question types, such as multiple choice and numerical answers.

Students must not receive answer keys before submission.

The AI question-generation integration is a placeholder for now.

## Service 5 — Grading

Language: Go.

Owns:

* Evaluation records.
* Deterministic scores.
* Rubric results.
* Draft feedback.
* Finalized grades.
* Teacher overrides.
* Grading audit history.

Implement deterministic grading for supported objective question types.

Use explicit grading rules.

Avoid floating-point equality for numerical answers where exactness or tolerance requirements can be represented more safely.

Teacher approval must remain mandatory for consequential grading decisions.

Publish finalized grading events only after the relevant transaction commits.

AI-assisted feedback remains unimplemented.

## Service 6 — Learning Analytics

Language: Python.

Owns:

* Derived performance metrics.
* Topic-level performance.
* Skill-level performance.
* Assessment trends.
* Weakness indicators.
* Evidence-backed analytical summaries.

Implement basic deterministic calculations using actual finalized assessment evidence.

For example:

```text
Student: Student 123

Skill: Fraction Addition

Questions attempted: 10
Correct answers: 6
Observed accuracy: 60%
```

This is an illustrative example, not sample data to hardcode into production responses.

Do not equate observed accuracy with a validated mastery probability.

Represent insufficient evidence explicitly.

Consume relevant Kafka events and maintain service-owned analytical read models.

AI-generated performance interpretations remain placeholders.

## Service 7 — AI Service

Language: Python.

This is the shared AI infrastructure service.

It will eventually own:

* Model-provider integrations.
* Model configuration.
* Structured generation.
* Embedding generation.
* Provider-specific error handling.
* Token and usage tracking.
* Prompt execution infrastructure.

For now:

* Implement its deployable application.
* Define versioned API contracts.
* Validate requests.
* Define typed response and error schemas.
* Implement health and readiness endpoints.
* Define provider-independent interfaces.
* Create placeholder implementations.

Do not integrate a real model provider.

Do not require API keys to run the initial architecture.

Do not fabricate AI output.

This service does not own educational business decisions.

---

# 7. THREE ADDITIONAL AI AGENT MICROSERVICES

Each AI agent must be a separate, independently deployable Python service.

Do not implement all three agents inside the shared AI Service.

The shared AI Service will eventually provide reusable model-inference infrastructure.

Each agent owns its own workflow orchestration, prompt definitions, input and output contracts, and capability-specific validation.

## Service 8 — Question Generation Agent

Language: Python.

Future responsibility:

Generate practice questions from authorized educational content and target skills.

Define a contract accepting:

* Authorized content references.
* Target skill identifiers.
* Question type.
* Difficulty.
* Question count.
* Optional authorized performance context.

Define a structured response contract for:

* Generated questions.
* Answer keys.
* Explanations.
* Source references.
* Validation metadata.

For this milestone, implement only the service foundation and integration contract.

Do not generate fake questions.

## Service 9 — Student Performance Agent

Language: Python.

Future responsibility:

Interpret validated performance data and generate personalized teacher-facing feedback.

Define contracts accepting:

* Authorized student reference.
* Validated performance summary.
* Assessment evidence references.
* Relevant curriculum skills.

The eventual output may include:

* Evidence-backed observations.
* Possible misconceptions.
* Suggested interventions.
* Recommended practice skills.

Do not allow this agent to invent grades or modify finalized results.

For now, implement only the service foundation and integration contract.

## Service 10 — Question Chatbot Agent

Language: Python.

Future responsibility:

Answer student questions using RAG over authorized educational materials.

Define contracts accepting:

* Authenticated student context.
* Authorized document references.
* Question.
* Conversation reference.
* Retrieved evidence references.

The future response must support source citations and explicitly indicate insufficient evidence.

For now, implement only the service foundation and integration contract.

Do not return generic LLM answers disguised as RAG-grounded responses.

---

# 8. STRICT PLACEHOLDER POLICY

This requirement is especially important.

AI functionality is intentionally out of scope for the first milestone.

However, AI integration contracts must be ready for future implementation.

For an AI operation that is not implemented, return an explicit, machine-readable response.

For example:

```json
{
  "error": {
    "code": "AI_CAPABILITY_NOT_IMPLEMENTED",
    "message": "Question generation is not available yet.",
    "capability": "question_generation"
  }
}
```

Use an appropriate HTTP status such as 501 for an explicitly unimplemented capability.

Distinguish this from a temporary outage or disabled feature.

Never return:

* Fabricated questions.
* Invented student performance.
* Fake embeddings.
* Fake chatbot answers.
* Hardcoded grading recommendations.
* Fake document citations.

Deterministic fakes may be used in automated tests if they are clearly identified as test doubles.

Do not enable test doubles as real production AI functionality.

Frontend controls for unavailable AI features must clearly communicate their status.

The UI must not present AI operations as working when they are placeholders.

Do not create empty methods that silently return success.

---

# 9. KONG API GATEWAY

Use Kong as the API gateway.

Implement reproducible configuration.

Responsibilities include:

* Public API routing.
* Request identification.
* Appropriate authentication enforcement.
* Rate limiting where supported.
* CORS.
* Request-size limits.
* Routing to the correct service.

Use a supported authentication architecture for the selected Kong edition.

Do not rely on unavailable Enterprise-only plugins.

Backend services must enforce their own resource-level authorization.

Do not expose private agent services directly to the public internet.

Public requests should reach relevant business services through Kong.

Internal agent-to-agent or service-to-agent communication should occur through private service networking and explicit contracts.

Protect the Kong Admin API.

All routing configuration must be reproducible from source control.

---

# 10. APACHE KAFKA

Use Kafka as the asynchronous message broker.

Start with a small set of meaningful business events.

Suggested initial topics:

```text
content.uploaded.v1

content.indexed.v1

assessment.submitted.v1

grading.finalized.v1

analytics.performance-updated.v1
```

Only publish an event when the corresponding business event actually occurred.

For example, do not publish `content.indexed.v1` before a document has genuinely completed indexing.

Use a consistent event envelope containing:

* Event ID.
* Event type.
* Schema version.
* Timestamp.
* Correlation ID.
* Relevant resource identifiers.
* Necessary event payload.

Implement:

* Consumer groups.
* Idempotent consumers.
* Explicit retry handling.
* Dead-letter handling for poison messages.
* Structured error logging.
* Transactional outbox publishing for database-backed events.

Assume at-least-once delivery.

Do not claim exactly-once business processing without implementing and demonstrating the necessary guarantees.

Do not use Kafka as a substitute for all HTTP APIs.

## Required initial event flow

Implement at least one real end-to-end asynchronous workflow:

```text
Student submits assessment
           |
           v
Assessment Service
           |
           v
assessment.submitted.v1
           |
           v
Grading Service
           |
           v
Deterministic grading
           |
           v
Teacher approval / finalization
           |
           v
grading.finalized.v1
           |
           v
Learning Analytics Service
           |
           v
Updated performance metrics
```

If approval is required, the grading service must not emit the finalized event before approval.

Test duplicate deliveries and consumer restarts.

Ensure analytics can be reconstructed from the appropriate durable source events or a documented reconciliation mechanism.

---

# 11. NEON POSTGRESQL AND DATA OWNERSHIP

Use Neon PostgreSQL as the target managed database platform.

Each business service owns its own persistence.

Use separate databases where practical, or isolated schemas with separate database roles and restricted permissions.

For local development, use PostgreSQL with an equivalent isolation model.

Example ownership:

```text
identity
  users
  organizations
  classrooms
  enrollments

curriculum
  subjects
  courses
  units
  topics
  skills

content
  documents
  document_chunks
  lesson_plans
  conversations

assessment
  questions
  assessments
  assignments
  submissions

grading
  evaluations
  finalized_grades
  feedback

analytics
  performance_metrics
  skill_statistics
```

These are illustrative logical data models, not mandatory exact table names.

Design schemas based on actual invariants and access patterns.

The AI services may remain stateless initially.

Do not create separate AI databases without a clear persistence requirement.

## Database rules

* No cross-service database queries.
* No cross-service foreign keys.
* No shared ORM models representing another service's private persistence.
* No distributed database transactions.
* Use stable resource identifiers across service boundaries.
* Use reproducible migrations.
* Use appropriate indexes and constraints.
* Use least-privilege credentials.
* Do not store real student information in development fixtures.

Do not make every service depend on a shared database superuser.

---

# 12. FUTURE RAG ARCHITECTURE

Neon PostgreSQL and pgvector will support the eventual RAG system.

Design the data ownership and integration points now.

Do not implement embedding inference or conversational AI yet.

The future pipeline will be:

```text
Teacher uploads document
           |
           v
Content Service
           |
           v
Document extraction and chunking
           |
           v
AI Service
           |
           v
Embedding generation
           |
           v
Content-owned vector storage
           |
           v
Authorized retrieval
           |
           v
Question Chatbot Agent
           |
           v
Grounded answer with citations
```

For this milestone:

* Implement real document ownership and metadata.
* Establish the document-processing lifecycle.
* Define chunk and source-provenance models where required.
* Define embedding and retrieval interfaces.
* Document embedding versioning and reindexing requirements.
* Prepare a migration strategy for pgvector.
* Implement authorization boundaries around content access.

Do not mark documents as indexed until an actual indexing implementation exists.

Do not create fake vectors.

Do not fabricate retrieved chunks.

Any future AI service requesting content must use an authorized Content Service API rather than bypassing data ownership.

---

# 13. CLEAN ARCHITECTURE AND CODING PRINCIPLES

Apply clean architecture pragmatically inside every service.

Use explicit boundaries between:

1. Domain logic.
2. Application use cases.
3. Infrastructure.
4. Transport and API handling.

The domain layer must not depend on HTTP frameworks, Kafka clients, AI SDKs, or database libraries.

Implement dependency inversion at meaningful boundaries.

Apply:

* Single Responsibility Principle.
* Open/Closed Principle where useful.
* Interface Segregation.
* Dependency Inversion.
* DRY.
* KISS.
* YAGNI.

Avoid:

* God classes.
* God services.
* Circular imports.
* Circular service dependencies.
* Hidden side effects.
* Unnecessary generic abstractions.
* Premature optimization.
* Large inheritance hierarchies.
* Excessive interfaces.
* Magic constants.
* Unchecked type assertions.
* Swallowed exceptions.
* Unbounded retries.
* Duplicate domain rules.
* Shared mutable global state.

Use descriptive names.

Keep functions focused.

Prefer composition over inheritance.

Make failure behavior explicit.

Do not create abstraction layers merely to make the code appear sophisticated.

## Shared libraries

A small shared package for transport contracts, correlation IDs, or observability conventions is acceptable.

Do not share domain entities or database models across services.

Avoid a shared internal library that forces every microservice to be deployed simultaneously.

Version externally consumed API and event contracts.

---

# 14. MONOREPO STRUCTURE

Use a monorepo with independently deployable services.

A suggested structure:

```text
ai-learning-platform/
│
├── apps/
│   └── web/
│
├── services/
│   ├── identity/
│   ├── curriculum/
│   ├── content/
│   ├── assessment/
│   ├── grading/
│   ├── analytics/
│   ├── ai-core/
│   ├── question-generation-agent/
│   ├── student-performance-agent/
│   └── question-chatbot-agent/
│
├── contracts/
│   ├── http/
│   └── events/
│
├── infra/
│   ├── kong/
│   ├── kafka/
│   ├── postgres/
│   └── storage/
│
├── deployments/
│   └── local/
│
├── docs/
│   ├── architecture/
│   ├── decisions/
│   └── development/
│
├── scripts/
│
├── .github/
│   └── workflows/
│
├── docker-compose.yml
├── Makefile
├── README.md
└── AGENTS.md
```

Treat this as a suggested structure.

Preserve useful existing conventions.

Each service should have its own dependency management, configuration, Dockerfile, and tests.

The root-level tooling should orchestrate common development operations without tightly coupling service implementations.

---

# 15. AUTHENTICATION AND AUTHORIZATION

Use a standards-based authentication solution.

Do not implement custom cryptographic authentication.

Support teacher and student roles.

Implement authorization for:

* Classroom access.
* Student enrollment.
* Document access.
* Assessment access.
* Answer-key visibility.
* Individual student performance access.
* Teacher feedback access.

Students must not access another student's private performance records.

Teachers must not access unrelated classrooms or organizations.

AI agent services must not gain unrestricted access merely because they operate inside the private network.

Service-to-service authentication must be explicitly designed.

Do not trust arbitrary identity headers.

Provide a safe local development authentication configuration.

Development authentication bypasses must never be enabled in production.

---

# 16. FRONTEND FOUNDATION

Build an Angular frontend connected to the real backend.

Implement minimal but functional interfaces for:

* Authentication.
* Classroom management.
* Curriculum browsing.
* Educational document upload.
* Manually authored assessments.
* Student assessment submission.
* Teacher grading review.
* Student performance dashboard.

AI-dependent features may have clearly labeled unavailable states.

Do not build a fake chatbot that returns canned answers.

Do not invent dashboard statistics.

Handle:

* Loading states.
* Empty states.
* Validation errors.
* Authentication failures.
* Authorization failures.
* Network failures.
* Service unavailability.

Use typed API contracts.

Keep components small and cohesive.

Use accessible form controls and semantic HTML.

Do not introduce unnecessary frontend state-management dependencies.

---

# 17. OBSERVABILITY AND OPERATIONS

Implement consistent observability across Go and Python services.

Use structured JSON logging.

Propagate correlation IDs across HTTP calls and Kafka events.

Record:

* Service name.
* Request ID.
* Correlation ID.
* Request duration.
* Error category.
* Relevant event IDs.

Never log authentication tokens, confidential documents, or unnecessary student information.

Implement liveness and readiness endpoints.

Readiness must reflect actual essential dependencies.

Avoid making unrelated downstream services mandatory for readiness unless the service truly cannot operate without them.

Configure graceful shutdown.

Kafka consumers should stop cleanly and avoid acknowledging messages before processing is safely completed.

HTTP servers must support bounded request timeouts.

Database connections and external clients must have explicit resource limits.

Keep initial observability infrastructure lightweight.

Document how distributed tracing could be introduced later.

---

# 18. TESTING REQUIREMENTS

Tests are mandatory.

Each service must have an independently runnable test suite.

## Unit tests

Test:

* Domain invariants.
* Input validation.
* Authorization decisions.
* State transitions.
* Deterministic grading.
* Analytics calculations.
* Placeholder behavior.

Do not mock the business logic being tested.

## Integration tests

Use real isolated infrastructure where relevant.

Test:

* PostgreSQL persistence.
* Database constraints.
* Service HTTP APIs.
* Kafka producers and consumers.
* Service authentication.
* Event idempotency.
* Cross-service contracts.

Do not claim that mocked Kafka tests constitute full Kafka integration testing.

## Contract tests

Verify:

* HTTP request and response schemas.
* Kafka event schemas.
* Error contracts.
* Agent placeholder contracts.
* AI Service placeholder contracts.

## End-to-end tests

Test at least one complete non-AI workflow.

For example:

```text
Teacher creates classroom
           |
           v
Teacher creates curriculum skill
           |
           v
Teacher creates assessment
           |
           v
Student submits answers
           |
           v
Grading Service evaluates answers
           |
           v
Teacher finalizes grades
           |
           v
Kafka delivers grading event
           |
           v
Analytics updates student performance
           |
           v
Teacher views performance dashboard
```

This workflow must use actual running services.

Do not bypass the API gateway in the browser-level E2E test.

Test at least one authorization failure and one duplicate-event scenario.

## AI placeholder tests

Verify that unimplemented AI endpoints:

* Validate their request contracts.
* Return the documented unimplemented error.
* Do not fabricate successful responses.
* Do not require real provider credentials.
* Do not access unauthorized data.

---

# 19. LOCAL DEVELOPMENT ENVIRONMENT

Provide a reproducible development environment.

Use Docker Compose for infrastructure and service orchestration.

The initial development setup should include:

* Kong.
* Kafka.
* PostgreSQL.
* Object storage if used.
* Required authentication infrastructure.
* All ten microservices.
* Angular frontend where practical.

Use compatible container images.

Pin meaningful versions.

Document all required environment variables.

Provide `.env.example`.

Do not commit credentials.

Allow local development using PostgreSQL without requiring a paid Neon account.

The production configuration must support Neon through environment-based configuration.

Include useful commands such as:

```bash
make setup
make up
make down
make test
make lint
make build
make check
```

Adapt these commands to the existing repository tooling where appropriate.

Do not add Make merely for aesthetic reasons if equivalent established tooling already exists.

A developer should be able to start the environment from a clean checkout by following the README.

---

# 20. IMPLEMENTATION ORDER

Implement incrementally in the following order.

## Phase 1 — Repository and architecture

Inspect existing code.

Document service boundaries.

Identify reusable components.

Define service responsibilities and data ownership.

Create the architecture decision records.

## Phase 2 — Infrastructure

Configure:

* Kong.
* Kafka.
* PostgreSQL.
* Local object storage if required.
* Authentication infrastructure.
* Environment management.

Verify that infrastructure starts correctly.

## Phase 3 — Ten service foundations

Create the ten independent applications.

Implement:

* Startup.
* Configuration validation.
* Health endpoints.
* Readiness endpoints.
* Structured logging.
* Error handling.
* Dockerfiles.
* Test infrastructure.

Make all ten services independently runnable.

Do not mistake this phase for project completion.

## Phase 4 — Core business functionality

Implement the essential non-AI operations in:

* Identity & Classroom.
* Curriculum.
* Content.
* Assessment.
* Grading.
* Learning Analytics.

Use real persistence and enforce ownership.

Prioritize the smallest coherent set of operations that supports the required end-to-end workflow.

## Phase 5 — Kafka integration

Implement the assessment submission and grading event flow.

Implement idempotent consumption.

Implement the transactional outbox where required.

Verify that duplicate delivery does not duplicate business effects.

## Phase 6 — AI integration contracts

Implement the AI Service and three agent services with meaningful request schemas and explicit unimplemented responses.

Create interfaces needed for future inference and RAG.

Do not integrate actual AI.

## Phase 7 — Angular integration

Connect the frontend to real backend APIs.

Implement the required non-AI workflows.

Clearly label unavailable AI functionality.

## Phase 8 — Verification and cleanup

Run the complete test suite.

Run static analysis.

Run format checks.

Build every service.

Verify the Docker environment.

Test the end-to-end workflow.

Review service boundaries.

Remove dead code and unnecessary abstractions.

Fix failures before declaring the milestone complete.

---

# 21. MANDATORY QUALITY GATES

Every Go service must pass applicable checks equivalent to:

```bash
go fmt ./...
go vet ./...
go test ./...
go build ./...
```

Use an appropriate maintained Go linter if configured.

Every Python service must pass appropriate checks for:

* Formatting.
* Linting.
* Type checking.
* Unit tests.
* Application startup.

Use tools such as Ruff, mypy or Pyright, and pytest where appropriate.

Angular must pass:

* Linting.
* Type checking.
* Unit tests.
* Production build.

Run integration tests against actual dependencies.

Do not suppress errors merely to make checks pass.

Do not weaken tests to conceal defects.

If a check cannot be executed, explain why.

Never claim a check passed unless it actually ran successfully.

---

# 22. DOCUMENTATION REQUIREMENTS

Create or update:

## README.md

Explain:

* Project overview.
* All ten services.
* Technology stack.
* Setup instructions.
* Required environment variables.
* Local development commands.
* Testing.
* Known limitations.

## Architecture documentation

Include:

* System context diagram.
* Microservice dependency diagram.
* Service ownership table.
* Database ownership.
* Kafka event flow.
* Authentication and authorization flow.
* Future RAG architecture.
* Future AI agent integration.

Use Mermaid diagrams where useful.

## Architecture Decision Records

Document significant decisions, including:

* Why microservices were chosen.
* Why Go was chosen for transactional services.
* Why Python was chosen for analytics and AI services.
* Why Kong was chosen as the gateway.
* Why Kafka was chosen as the broker.
* Why Neon PostgreSQL and pgvector were chosen.
* Why AI functionality is deferred.
* How service data isolation is implemented.

## AGENTS.md

Create instructions for future Codex sessions.

Include:

* Architecture boundaries.
* Coding conventions.
* Service ownership.
* Testing requirements.
* Quality gates.
* Security requirements.
* Rules for adding new services.
* Rules for implementing future AI agents.

Future AI implementations must respect existing service contracts and data ownership.

---

# 23. DEFINITION OF DONE

The initial milestone is complete only when:

1. All ten microservices can start independently.
2. Kong routes public requests correctly.
3. Kafka is operational.
4. PostgreSQL persistence works.
5. Service data ownership is enforced.
6. Authentication and authorization work.
7. Core classroom and curriculum operations work.
8. Teachers can upload supported content.
9. Teachers can create supported assessments.
10. Students can submit answers.
11. Deterministic grading works.
12. Teachers can finalize grades.
13. Kafka delivers the relevant business events.
14. Analytics calculates actual student performance.
15. The frontend displays real backend data.
16. All three agent services expose validated integration contracts.
17. AI-dependent operations return explicit unimplemented responses.
18. Unit and integration tests pass.
19. The required E2E workflow passes.
20. Documentation matches the actual implementation.

If a requirement is not completed, report it explicitly.

Do not claim the platform is production-ready merely because it runs locally.

---

# 24. FINAL ENGINEERING REVIEW

After implementation, critically inspect your own work.

Look for:

* Circular service dependencies.
* Cross-service database access.
* Leaky abstractions.
* Unnecessary shared libraries.
* Duplicated business rules.
* Missing authorization checks.
* Incorrect Kafka acknowledgment behavior.
* Missing consumer idempotency.
* Unsafe database transactions.
* Unvalidated API inputs.
* Inconsistent error contracts.
* Unbounded retries.
* Sensitive data in logs.
* Fake AI functionality.
* Misleading readiness checks.
* Untested failure cases.

Fix actual defects.

Do not refactor merely to introduce more abstraction.

Preserve working behavior.

---

# 25. FINAL DELIVERABLE

At completion, provide an engineering handover containing:

1. The architecture actually implemented.
2. The ten services and their responsibilities.
3. The rationale for every language choice.
4. The actual repository structure.
5. The working non-AI features.
6. The AI placeholders and their contracts.
7. The Kafka topics and implemented event flows.
8. The database ownership model.
9. The test commands executed and their actual results.
10. Known limitations and unresolved risks.
11. The recommended next implementation milestone.

Distinguish clearly between implemented functionality, defined interfaces, test doubles, and future work.

---

# FINAL EXECUTION DIRECTIVE

Begin by inspecting the existing repository.

Preserve existing work wherever possible.

Implement the specified ten-microservice architecture incrementally.

Prioritize correct service boundaries, independent deployment, secure data ownership, reliable Kafka communication, and real non-AI functionality.

Establish clean interfaces for future AI capabilities without implementing or fabricating AI behavior.

Do not introduce unnecessary languages, frameworks, services, or infrastructure.

Do not stop after generating scaffolding.

Run and report the actual quality gates.

If implementation constraints prevent full completion, finish a coherent, working subset, identify precisely what remains incomplete, and do not falsely claim the definition of done has been met.

**Deliver a functioning, testable microservices foundation that can accommodate the three dedicated AI agents without requiring a fundamental architectural rewrite.**
