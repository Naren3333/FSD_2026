# Scalability

The system is built to scale out: any application can run as several replicas behind the gateway with no code or
configuration change beyond the replica count. `docker-compose.scale.yml` runs three replicas of Identity, Assessment
and Grading, and `python scripts/stack_tests.py scale` (`scripts/scale_demo.py`) load-tests them on the disposable
test stack. This page lists what makes that work, what the demo shows, and what a real deployment would still
change. Production hardening (TLS, secrets management, Keycloak in production mode, broker ACLs, backups) is a
separate concern, covered in `docs/development/handover.md`, not here.

## What makes replicas safe

| Property | How | Where |
|---|---|---|
| Any replica can serve any request | Applications keep no session or business state in memory; each request carries its bearer token and every record lives in the owning service's database. In-process caches are operational only (signing keys, Content's query vectors) and a miss is just slower. | `packages/platform/auth.go`, `services/content/vectors.go` |
| The gateway spreads requests | Kong routes to each service by its Compose name and re-resolves it every 5 s, and Compose's DNS answers with every running replica, so new replicas get traffic and stopped ones stop getting it. | `infra/kong/bootstrap.py`, `KONG_DNS_VALID_TTL` in `docker-compose.yml` |
| Events are published once and in order, from any replica | Every replica runs an outbox relay. Each locks its batch with `FOR UPDATE SKIP LOCKED`, so two never publish the same row, and an event waits while an older one with the same key is unpublished, so one resource's events keep their order. | `packages/platform/outbox.go` (`dueRows`) |
| Consumers share the work | Each service reads each topic as one Kafka consumer group, so the topic's partitions are divided among its replicas and handed over when one joins or leaves. Offsets are committed only after the event's transaction commits. | `packages/platform/consumer.go` |
| A redelivered event changes nothing | Each consumer records the event id in its inbox in the same transaction as its effect, so an event delivered again (after a replica dies mid-event, or a rebalance) is a no-op. Analytics also deduplicates by evaluation. | `packages/platform/consumer.go` (`handleOnce`), `services/analytics` |
| Background jobs are leased | Content's indexing jobs and Grading's AI marking jobs are leased with `FOR UPDATE SKIP LOCKED` and a lease time, so replicas take different jobs and a job whose replica died is taken over when its lease expires. | `services/content/index.go`, `services/grading/marker.go` |
| Replicas can start together | Migrations run under a PostgreSQL advisory lock, so only one replica applies them. | `packages/platform/migrate.go`, `learning_transport/db.py` |
| Replicas can stop cleanly | On SIGTERM a Go service stops taking requests, finishes those in flight and stops its workers; ai-core waits for ledger writes. | `packages/platform/app.go` |
| Load per replica is bounded | Each replica has at most 10 database connections and each Python one at most 100 requests in flight; Kong limits each signed-in session to 120 requests a minute. | `packages/platform/db.go`, the Python Dockerfiles, `infra/kong/bootstrap.py` |
| Services fail independently | Each service owns its database and reaches others only through their APIs or events, so a slow or failed consumer delays its own work without blocking the services that publish to it. | `docs/architecture/system.md` |

## The demo

`python scripts/stack_tests.py scale` starts a fresh test stack with `docker-compose.scale.yml`, creates 200
students in one classroom, and runs the same load three times, each on a new auto-marked assessment:

1. Identity, Assessment and Grading at one replica each.
2. The same at three replicas each.
3. Three replicas again, with one Grading replica killed (`docker kill`) as the burst begins.

The load (`scripts/load/deadline.js`, run with k6 in a container on the stack's network) is a class at its deadline:
every student submits at the same moment while the class keeps reading its assessment list, the assessment, its own
submission and its own results at a steady rate. Each submission goes through Kong, is authorized at Identity,
stored by Assessment with its event in the outbox, published to Kafka, and marked by a Grading consumer.

Each replica is capped at `REPLICA_CPUS` (a quarter of a CPU by default), as if it were a small node of its own.
Without the cap, three replicas on one computer would share the same CPUs as one, and the comparison would show nothing.

After each run the script checks what must hold whatever the replica count: every submission accepted once, every
one marked exactly once, the outbox drained and Grading's consumers without lag. It fails if any of these does not
hold. It prints latency, throughput, the time to mark everything, the requests each replica served (from the
services' request logs) and which replica holds each Kafka partition, and writes the report to
`.local/scale-demo/report.md`.

`stack_tests.py scale` uses the defaults: 200 students, 600 reads a second for 30 s from 4 sessions per student, and
a quarter of a CPU per replica. To change them, keep the stack (`stack_tests.py scale --keep`) and run the script
again against it with the test stack's settings, for example in PowerShell:

```powershell
$env:COMPOSE_PROJECT_NAME="learning-test"; $env:KONG_PORT="18000"; $env:KEYCLOAK_PORT="18081"
$env:LOCAL_CONFIG_DIR="./.local/test-stack"; $env:COMPOSE_FILE="docker-compose.yml;docker-compose.scale.yml"
py -3.13 scripts/scale_demo.py --students 300 --browse-rate 900 --sessions 4
```

`REPLICA_CPUS` takes effect when the containers are created, so set it before `stack_tests.py scale` starts the
stack.

What it shows, and what it does not:

- **Shown:** replicas take a share of the requests and of the event partitions; the system stays correct with
  replicas added, removed or killed, and a killed Grading replica's partition is taken over by another replica, its
  submissions marked late but exactly once; when each replica is a fixed slice of CPU and the computer has CPUs to
  spare, adding replicas adds capacity.
- **Depends on the computer:** on a 4-CPU cloud container (2026-10-19), one replica of each fell behind the
  reads (p50 832 ms, a third of them not started) and three kept up (p50 102 ms), with every check passing. A
  computer with fewer spare CPUs than the replicas need shows less difference, because the computer is the limit.
- **A killed replica costs a few requests.** Requests Kong had already sent to it fail, and Kong stops routing to
  it once it re-resolves the name (within 5 s): one read of about 17,000 failed in the run above.
- **Not shown:** production capacity. Everything, the load generator included, runs on one computer and one
  PostgreSQL, and the numbers depend on that computer. The AI paths are not load tested: they make paid calls and
  are bounded by the provider's rate limits and each user's daily allowance (ADR 0003), not by replicas.

## Limits of this setup

- **One PostgreSQL server.** Every service has its own database and role, but they share one server, which is the
  ceiling for the whole system and its single point of failure. Replicas also multiply connections:
  `docker-compose.scale.yml` raises `max_connections` to 300 for that reason.
- **One Kafka broker, three partitions per topic, replication factor 1.** A consumer group uses at most three
  consumers per topic, so a fourth Grading replica would idle on submissions, and a broker failure stops event flow
  (the outboxes keep every event until it returns).
- **Failover waits for Kafka's session timeout.** A replica that dies without leaving the group keeps its partitions
  until the group's session timeout (kafka-go's default, 30 s) expires; only then does another replica take them, so
  that share of the work is late, not lost.
- **One Kong and one Keycloak.** Kong's rate limit is counted per Kong node (`policy: local`); Keycloak runs
  `start-dev`.
- **Per-process counters.** ai-core limits each user's concurrent paid calls per process, so with more replicas a
  user can overshoot their daily allowance by a few more calls (the allowance itself is checked in the shared
  ledger). Content's query-vector cache is per replica.
- **No metrics.** There is no Prometheus or tracing; the demo measures from the load generator, `docker stats` and
  the databases.

## What a real deployment would change

- A managed PostgreSQL per service (or per group of small services), with a connection pooler such as PgBouncer and
  read replicas for the read-heavy services (Analytics, the teacher's lists). Each service already connects only
  through its own `DATABASE_URL`, so moving one is configuration.
- Three Kafka brokers with replication factor 3, and more partitions on the busy topics
  (`assessment.submitted.v4`) so more consumers can share them.
- Kong behind a load balancer as several nodes, with the rate limit counted in Redis; Keycloak as a production
  cluster.
- An orchestrator such as Kubernetes running the replicas, with autoscaling on CPU or consumer lag, readiness probes
  on `/health/ready` (already there), and metrics and tracing.
