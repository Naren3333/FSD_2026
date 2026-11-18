# 0018 — Platform monitoring and operator access

## Context

Operators need one view of service readiness, request latency, dead-letter retention, and recorded AI expenditure. The ten learning and AI applications retain their existing responsibilities and databases.

## Decision

Run the collector as stateless operational infrastructure alongside Kong and Kafka. It probes the ten applications privately, reads Kafka partition offsets for dead-letter topics, and calls ai-core's internal spend reporting API. Ai-core keeps sole ownership of the usage ledger. Monitoring makes no model calls and stores its rolling samples in memory.

Kong exposes three read operations under `/api/monitoring`, documented in the monitoring HTTP contract. Access requires a validated JWT and a dedicated human account with the Keycloak `platform-operator` role. The Go service embeds the operator role policy, which also generates the browser's routing rules. Teacher, student, and service roles cannot use the operator API. The separate `monitoring-reader` client has access to private latency and spend reads through a client-credentials token. Billed-account reports contain cross-organization ledger identifiers and are served with `Cache-Control: no-store`. A successfully served report generates a log entry with the operator subject and correlation ID.

The private health endpoints report process liveness and recent collection progress. Individual dependency failures appear in the operator snapshot. Each process supplies the latency bucket boundaries with its counters, and the collector accepts samples only when the boundaries match its aggregation schema. Mixed-version producers without the new field report unavailable latency until replaced.

## Consequences

The operator role and monitoring-reader secret require provisioning in existing environments. ai-core builds its date index concurrently so existing ledger writers can keep running during a rolling upgrade. See [Platform monitoring](../development/operations.md#platform-monitoring) for provisioning and deployment steps.
