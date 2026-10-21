-- deleting a classroom now announces identity.classroom.deleted.v1 so every service purges its data for it.
CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now());
WITH deleted AS (SELECT id, org_id, gen_random_uuid() AS event_id FROM classrooms WHERE deleted_at IS NOT NULL)
INSERT INTO outbox(id, topic, partition_key, body)
SELECT event_id, 'identity.classroom.deleted.v1', id::text, jsonb_build_object(
  'event_id', event_id,
  'event_type', 'identity.classroom.deleted.v1',
  'schema_version', 1,
  'timestamp', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'correlation_id', gen_random_uuid(),
  'resource_id', id,
  'data', jsonb_build_object('classroom_id', id, 'org_id', org_id))
FROM deleted;
