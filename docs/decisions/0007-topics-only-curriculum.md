# ADR 0007: The curriculum is a flat list of topics

Status: accepted 2026-10-18. The project owner decided to cut the unused curriculum levels ("Cut the levels"), including the destructive migration this needs, and then to call the one remaining level "topic" everywhere, because having both "topic" and "skill" name the same thing was confusing.

## Context

Curriculum stored a tree of six kinds of node (subject, course, unit, topic, skill, learning objective). Only skills were used anywhere: assessment questions, grading evidence, analytics and question generation all named a skill, and nothing read the levels above it. Teachers still had to choose a level and a parent for every node, and archiving was refused while a node had children.

## Decision

**Curriculum has one level, the topic.** A topic is `{id, name}` with no kind and no parent. Prerequisites link topics to topics. The API is `GET/POST /v1/topics`, `GET/PATCH/DELETE /v1/topics/{id}` and `POST /v1/topics/{id}/prerequisites` (`{topic_id, prerequisite_id}`); `/v1/nodes` is removed. Archived topics are listed with `GET /v1/topics?archived=true` and made active again with `POST /v1/topics/{id}/restore`. The Curriculum page is one list of topics with an add form and an archive action.

**The word "skill" is gone from new contracts.** Every `skill_id` is `topic_id`; error codes are `UNKNOWN_TOPIC` (question generation) and `INVALID_TOPIC` (Assessment); Analytics returns `topics` and `TopicMetric`; the web app says "topic".

**Events get new versions; old ones stay readable.** Assessment publishes only `assessment.submitted.v4` (v3 with `questions[].topic_id`), and Grading publishes only `grading.finalized.v3` (v2 with `evidence[].topic_id`). Grading also keeps consuming v1–v3 submissions and Analytics v1–v2 finalized grades, reading their `skill_id` as the topic, because old events may still be queued or replayed. Old event schemas are unchanged.

**Stored data is renamed by each owner, in its own migration.**
- Curriculum `002_topics.sql` (destructive, authorized 2026-10-18): deletes every node that is not a skill and its prerequisite links, drops `parent_id` and `kind`, renames `nodes` to `topics` and `prerequisites.skill_id` to `topic_id`. Existing skills keep their ids and names as topics.
- Assessment and Grading `004_topics.sql`: rename the `skill_id` key to `topic_id` inside stored question and evidence JSON. Analytics `004_topics.sql`: renames `evidence.skill_id` to `topic_id`. Outbox and inbox rows are not rewritten.

## Consequences

- Subjects, courses, units and learning objectives created before 2026-10-18 are deleted and cannot be recovered from the database. Nothing outside Curriculum referred to them.
- The new Kafka topics (and their `.dlq`) are created by `kafka-init`, so an existing stack needs `docker compose up -d --build`.
- Grouping topics (by subject or course) can come back later as an additive change if teachers need it.
